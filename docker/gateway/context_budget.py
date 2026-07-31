"""Orçamento de contexto por request: clamp de max_tokens à janela real do
modelo (machines.max_model_len, migration 0031) e rejeição clara quando nem o
prompt cabe.

Motivação: o gateway aplicava só piso/teto fixos (MIN/MAX_MAX_TOKENS) sem
conhecer o --max-model-len do vLLM que vai atender — um cliente de prompt
grande (Claude Code manda ~26k tokens só de system+tools) recebia o 400 cru
do vLLM ("This model's maximum context length is 16384 tokens..."). Aqui o
gateway reserva espaço pro prompt estimado e entrega o restante da janela como
teto de saída; se nem o prompt couber, devolve um erro acionável no formato do
cliente (Anthropic ou OpenAI, via exception handler no main.py).

Módulo separado do main.py de propósito: funções puras, importáveis pelos
testes sem as env vars obrigatórias (SUPABASE_URL etc.) do main.
"""

import json
import math
import os
from dataclasses import dataclass
from enum import Enum

from fastapi import HTTPException

# Margem sobre a estimativa de prompt: a heurística ~4 chars/token subestima
# texto denso (código tokeniza mais "caro"); o json.dumps já infla um pouco
# (aspas/escapes), mas não o bastante pra dispensar folga.
CONTEXT_SAFETY_FACTOR = float(os.environ.get("CONTEXT_SAFETY_FACTOR", "1.2"))
# Margem quando o número NÃO é estimativa: veio do tokenizer do modelo via
# /tokenize. Aqui não há erro de estimativa a cobrir — só o resíduo entre o
# texto que contamos (json.dumps de messages+tools) e o que o vLLM vai
# realmente montar pelo chat template. Esse resíduo tende a favor: o JSON
# acrescenta chaves/aspas/escapes que o template não tem, e os marcadores de
# role/BOS/EOS que o template acrescenta já estão cobertos por
# CONTEXT_TEMPLATE_OVERHEAD. 2% é folga pro resto.
#
# Aplicar 1.2 aqui era o bug que quebrava o /compact do Claude Code: 122658
# tokens EXATOS numa janela de 131072 reservavam 147390 e o gateway rejeitava
# um prompt que cabia com ~8k de sobra pra resposta — e /compact, que reenvia
# a transcrição inteira, é justamente o pedido que morria nessa conta.
CONTEXT_EXACT_SAFETY_FACTOR = float(os.environ.get("CONTEXT_EXACT_SAFETY_FACTOR", "1.02"))
# Margem quando a contagem exata foi TENTADA e falhou (pod fora do ar, agent
# antigo sem /tokenize, timeout). Não dá pra usar 1.02 (o número é heurística,
# não contagem) nem 1.2 (o cliente já foi instruído a mandar até
# auto_compact_window, que só é admissível com contagem exata — 1.2 rejeitaria
# de forma intermitente, dependendo da saúde do pod). Meio-termo deliberado.
CONTEXT_FALLBACK_SAFETY_FACTOR = float(os.environ.get("CONTEXT_FALLBACK_SAFETY_FACTOR", "1.1"))
# Saída garantida a reservar da janela ao recomendar quanto de input o cliente
# pode mandar (ver usable_input_tokens). Mesma env var do piso de max_tokens do
# main.py, que reimporta daqui: mora neste módulo porque é ele que calcula a
# janela recomendada e é o único importável sem as env vars do main.
RESERVED_OUTPUT_TOKENS = int(os.environ.get("MIN_MAX_TOKENS", "8000"))
# Tokens do chat template (marcadores de role, BOS/EOS) que não aparecem no
# corpo JSON mas ocupam janela no vLLM.
CONTEXT_TEMPLATE_OVERHEAD = int(os.environ.get("CONTEXT_TEMPLATE_OVERHEAD", "200"))
# Abaixo disso não há resposta útil possível — melhor rejeitar com instrução
# de compactar do que devolver meia dúzia de tokens truncados.
MIN_VIABLE_COMPLETION_TOKENS = int(os.environ.get("MIN_VIABLE_COMPLETION_TOKENS", "256"))
# Fração da janela a partir da qual a estimativa heurística (que já embute
# CONTEXT_SAFETY_FACTOR) é considerada "perto o suficiente" do limite pra
# valer a pena pagar uma chamada de rede extra (gateway -> agent -> vLLM
# /tokenize) e decidir aceitar/rejeitar com a contagem real do tokenizer, em
# vez de confiar só na heurística. Requisições longe do teto nunca pagam
# esse custo — ver should_use_exact_token_count.
CONTEXT_EXACT_COUNT_THRESHOLD = float(os.environ.get("CONTEXT_EXACT_COUNT_THRESHOLD", "0.7"))
# Custo em tokens de janela de UMA imagem. O base64 fica fora da heurística de
# chars/4 (ver _strip_images) porque superestimaria absurdamente — mas contar
# ZERO subestima igualmente: num modelo VL a imagem ocupa janela de verdade
# (~1k-3k tokens no Qwen3-VL, dependendo da resolução, que não conhecemos aqui).
# Zero fazia o prompt perto do teto passar pelo orçamento e voltar como um 400
# de contexto do vLLM — que, num cliente que reenvia a conversa toda, envenena
# a sessão inteira (ver docker/gateway/content_policy.py).
# Estimativa fixa e deliberadamente grosseira: perto do limite quem decide é a
# contagem real do tokenizer (should_use_exact_token_count).
CONTEXT_IMAGE_TOKENS = int(os.environ.get("CONTEXT_IMAGE_TOKENS", "1600"))


class EstimateKind(str, Enum):
    """De onde veio o número de tokens do prompt — determina quanta margem
    reservar em cima dele. São três casos, não dois: "tentou contar e falhou"
    não pode ser tratado nem como estimativa crua nem como contagem exata."""

    HEURISTIC = "heuristic"  # longe do limite, nunca tentou contar
    EXACT = "exact"  # /tokenize do vLLM respondeu
    FALLBACK = "fallback"  # tentou contar e falhou (pod/agent/timeout)


_SAFETY_FACTORS = {
    EstimateKind.HEURISTIC: CONTEXT_SAFETY_FACTOR,
    EstimateKind.EXACT: CONTEXT_EXACT_SAFETY_FACTOR,
    EstimateKind.FALLBACK: CONTEXT_FALLBACK_SAFETY_FACTOR,
}


class ContextWindowExceeded(HTTPException):
    """Prompt não deixa espaço viável de resposta na janela do modelo.

    Subclasse de HTTPException de propósito: os `except HTTPException` dos
    call sites (release_flight) continuam funcionando, e o exception handler
    registrado no main.py (resolvido por MRO, vence o default do FastAPI)
    formata o corpo no shape do cliente — Anthropic em /v1/messages*, OpenAI
    no resto."""

    def __init__(self, message: str):
        super().__init__(status_code=400, detail=message)


def anthropic_error_body(message: str) -> dict:
    """Shape de erro da Anthropic Messages API (o que o Claude Code exibe)."""
    return {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": message},
    }


def openai_error_body(message: str) -> dict:
    """Shape de erro OpenAI, com o code canônico de estouro de contexto."""
    return {
        "error": {
            "message": message,
            "type": "invalid_request_error",
            "code": "context_length_exceeded",
        }
    }


def _strip_images(messages: list) -> tuple[list, int]:
    """Remove partes image_url do cálculo de texto e devolve quantas eram.

    O base64 sai da heurística de chars/4 porque superestimaria absurdamente
    (imagem não tokeniza como texto), mas a CONTAGEM volta pelo chamador como
    CONTEXT_IMAGE_TOKENS cada — antes eram descartadas e valiam zero, o que
    subestimava a janela ocupada."""
    out = []
    images = 0
    for m in messages:
        if isinstance(m, dict) and isinstance(m.get("content"), list):
            kept = []
            for part in m["content"]:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    images += 1
                    continue
                kept.append(part)
            m = {**m, "content": kept}
        out.append(m)
    return out, images


def count_images(messages) -> int:
    """Quantas partes image_url há no prompt. Público porque quem chama o
    /tokenize precisa somar o custo delas de volta: o texto mandado pro
    tokenizer sai de prompt_text_for_tokenize, que descarta as imagens de
    propósito (ver resolve_est_tokens)."""
    if not isinstance(messages, list):
        return 0
    return _strip_images(messages)[1]


def prompt_text_for_tokenize(messages=None, tools=None, extra_texts=()) -> str:
    """Texto usado tanto pela heurística de chars/4 quanto, perto do limite,
    como "prompt" na contagem real via /tokenize do vLLM (ver
    should_use_exact_token_count) — um único lugar monta esse texto pra não
    ter duas versões ligeiramente diferentes do que está sendo contado.

    Só TEXTO: as imagens saem daqui e entram na conta por
    CONTEXT_IMAGE_TOKENS em estimate_prompt_tokens."""
    parts = []
    if isinstance(messages, list):
        text_only, _ = _strip_images(messages)
        parts.append(json.dumps(text_only, ensure_ascii=False))
    if tools:
        parts.append(json.dumps(tools, ensure_ascii=False))
    parts.extend(text for text in extra_texts if isinstance(text, str))
    return "".join(parts)


def estimate_prompt_tokens(messages=None, tools=None, extra_texts=()) -> int:
    """Heurística ~4 chars/token sobre o corpo já em formato OpenAI. Conta
    também as tools (nos clientes agênticos são a maior fatia do prompt — o
    count_tokens antigo ignorava e o Claude Code compactava tarde demais) e
    textos avulsos ("prompt" do /v1/completions, "instructions"/"input" da
    Responses API).

    ensure_ascii=False (dentro de prompt_text_for_tokenize): com o default,
    cada caractere acentuado vira ç (6 chars) e texto em português — o
    público do produto — é superestimado em ~2x, clampando saída de prompts
    que cabem e fazendo o count_tokens mandar o Claude Code compactar na
    metade útil da janela.

    Imagens entram por CONTEXT_IMAGE_TOKENS cada, não pela heurística de
    chars (o base64 não tokeniza como texto) — e não por zero, que era o
    comportamento anterior e subestimava a janela ocupada."""
    text = prompt_text_for_tokenize(messages, tools, extra_texts)
    images = _strip_images(messages)[1] if isinstance(messages, list) else 0
    return max(1, len(text) // 4 + images * CONTEXT_IMAGE_TOKENS)


def reserved_tokens_for(est_tokens: int, kind: EstimateKind = EstimateKind.HEURISTIC) -> int:
    """Custo real reservado na janela para um prompt de est_tokens tokens:
    margem de segurança (que depende de ONDE o número foi obtido — ver
    EstimateKind) + overhead de chat template. Única fonte de verdade dessa
    conta — usada tanto por apply_context_budget (decide rejeição) quanto por
    should_use_exact_token_count (decide se vale a pena trocar a heurística
    pela contagem real).

    O default é HEURISTIC de propósito: é o que should_use_exact_token_count
    está avaliando por construção (ainda não contou nada), e preserva o
    comportamento de qualquer chamador que não passe kind."""
    return math.ceil(est_tokens * _SAFETY_FACTORS[kind]) + CONTEXT_TEMPLATE_OVERHEAD


def should_use_exact_token_count(est_tokens: int, machine: dict) -> bool:
    """Fast-path: a maioria das requisições fica longe do teto e não deve
    pagar o custo de uma chamada de rede extra ao agent/vLLM só pra confirmar
    o que a heurística já mostra sobrar de sobra. Só quando a estimativa (que
    já é pessimista, incluindo CONTEXT_SAFETY_FACTOR) encosta perto da janela
    real da máquina é que vale a pena pedir a contagem exata do tokenizer."""
    max_model_len = machine.get("max_model_len")
    if not isinstance(max_model_len, int) or max_model_len <= 0:
        return False  # sem janela conhecida, apply_context_budget já é no-op
    return reserved_tokens_for(est_tokens) >= max_model_len * CONTEXT_EXACT_COUNT_THRESHOLD


async def resolve_est_tokens(
    machine: dict,
    heuristic_est: int,
    exact_text: str,
    tokenize_fn,
    image_tokens: int = 0,
) -> tuple[int, EstimateKind]:
    """Decide o est_tokens final pro apply_context_budget, e de onde ele veio.

    Heurística por padrão (fast-path — a maioria das requests fica longe do
    teto e não deve pagar uma chamada de rede pra confirmar o que já sobra de
    sobra); contagem real do tokenizer via /tokenize do vLLM só quando
    should_use_exact_token_count indica proximidade do limite.

    image_tokens é somado à contagem exata porque exact_text sai de
    prompt_text_for_tokenize, que DESCARTA as imagens (mandar base64 pro
    tokenizer seria uma chamada enorme e uma contagem errada). Sem essa soma,
    perto do limite um prompt com imagem passava a valer MENOS do que valia
    pela heurística — exatamente o que CONTEXT_IMAGE_TOKENS existe pra evitar.

    tokenize_fn é injetado (nunca importado) pra este módulo seguir puro e
    testável sem as env vars obrigatórias do main.py."""
    if not should_use_exact_token_count(heuristic_est, machine):
        return heuristic_est, EstimateKind.HEURISTIC
    model = machine.get("served_model_name") or machine.get("model_name")
    if not model:
        return heuristic_est, EstimateKind.FALLBACK
    exact = await tokenize_fn(machine, model, exact_text)
    if exact is None:
        return heuristic_est, EstimateKind.FALLBACK
    return exact + image_tokens, EstimateKind.EXACT


def usable_input_tokens(max_model_len: int, reserved_output: int | None = None) -> int:
    """Maior prompt que ainda deixa reserved_output tokens de saída na janela.

    Assume contagem exata (CONTEXT_EXACT_SAFETY_FACTOR): é o número que o
    CLIENTE usa pra decidir se manda ou compacta, e um prompt desse tamanho
    cai na faixa em que o gateway sempre escala pro tokenizer real."""
    reserved_output = RESERVED_OUTPUT_TOKENS if reserved_output is None else reserved_output
    room = max_model_len - reserved_output - CONTEXT_TEMPLATE_OVERHEAD
    return max(0, int(room / CONTEXT_EXACT_SAFETY_FACTOR))


def auto_compact_window(max_model_len: int) -> int:
    """Valor de CLAUDE_CODE_AUTO_COMPACT_WINDOW, arredondado pra baixo em 1k.

    ATENÇÃO à semântica, que não é óbvia: este valor NÃO é "compacte ao chegar
    aqui". É a CAPACIDADE que o cliente passa a assumir (ele assume 200k pra
    qualquer ANTHROPIC_BASE_URL customizado e não tem como descobrir a real).
    O auto-compact dispara numa fração INTERNA dele — algo entre ~80% e ~92%,
    varia por versão do Claude Code e não é configurável.

    Daí a conta: pra compactação cair por volta de 80% da janela real, este
    número tem que ser o MAIOR admissível, não 80% da janela. Em 131072 →
    120000, e a compactação cai entre ~96k e ~110k, ou seja 73%-84% da janela.
    Declarar 80% (104000) faria compactar em 63%-73% — cedo demais, deixando
    ~15% de contexto na mesa por sessão.

    O teto é usable_input_tokens porque acima dele o próprio gateway rejeita
    (não sobraria espaço pra resposta) — recomendar mais que isso recriaria o
    bug que este módulo existe pra evitar. É por máquina, não por plano."""
    return (usable_input_tokens(max_model_len) // 1000) * 1000


def context_exceeded_message(est_tokens: int, max_model_len: int, kind: EstimateKind) -> str:
    """Mensagem do 400 de contexto.

    Não manda usar /compact de propósito: /compact reenvia a transcrição
    INTEIRA, então é justamente o pedido que dispara este erro — mandar o
    usuário usá-lo era um beco sem saída (a sessão travava sem saída nenhuma).
    O que resolve é sessão nova + configurar o cliente pra compactar antes de
    chegar aqui."""
    origem = "contagem do tokenizer" if kind is EstimateKind.EXACT else "estimado"
    return (
        f"o prompt ocupa ~{est_tokens} tokens ({origem}) e a janela de contexto do "
        f"seu plano é de {max_model_len} tokens — não sobra espaço para a resposta. "
        "Comece uma sessão nova (/clear no Claude Code) ou remova arquivos anexados. "
        "Para o cliente compactar sozinho antes de chegar neste ponto, configure "
        f"CLAUDE_CODE_AUTO_COMPACT_WINDOW={auto_compact_window(max_model_len)}"
    )


@dataclass(frozen=True)
class PromptBudget:
    """O que apply_context_budget concluiu sobre a request. Devolvido (em vez
    de só mutar o body) porque o caminho Anthropic precisa do est_tokens pra
    emitir usage.input_tokens no message_start — sem isso o rastreador de
    contexto do Claude Code fica em zero e o auto-compact nunca dispara."""

    est_tokens: int
    kind: EstimateKind
    max_model_len: int | None  # None = janela desconhecida (clamp foi no-op)
    reserved: int
    output_budget: int | None  # None junto com max_model_len


def apply_context_budget(
    body_json: dict,
    machine: dict,
    field: str = "max_tokens",
    est_tokens: int = 0,
    kind: EstimateKind = EstimateKind.HEURISTIC,
) -> PromptBudget:
    """Clampa body_json[field] ao que sobra da janela após reservar o prompt.

    Roda DEPOIS do piso/teto fixos (MIN/MAX_MAX_TOKENS) e pode ficar abaixo do
    piso — senão o piso re-inflaria o valor e o 400 do vLLM voltaria.
    machines.max_model_len ausente (template sem --max-model-len, pod anterior
    à migration 0031) → no-op de clamp: o vLLM usa a janela nativa do config do
    modelo, que o gateway não tem como conhecer; chutar seria pior que não
    clampar."""
    max_model_len = machine.get("max_model_len")
    reserve = reserved_tokens_for(est_tokens, kind)
    if not isinstance(max_model_len, int) or max_model_len <= 0:
        return PromptBudget(est_tokens, kind, None, reserve, None)
    budget = max_model_len - reserve
    if budget < MIN_VIABLE_COMPLETION_TOKENS:
        raise ContextWindowExceeded(context_exceeded_message(est_tokens, max_model_len, kind))
    current = body_json.get(field)
    if isinstance(current, int) and current > budget:
        body_json[field] = budget
    return PromptBudget(est_tokens, kind, max_model_len, reserve, budget)
