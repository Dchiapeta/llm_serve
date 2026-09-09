"""Qual prompt configurado vale pra uma request — o da STACK ou o da CHAVE.

O `system_prompt` da stack (migration 0020) é a instrução do produto inteiro:
uma stack, uma personalidade. Só que a mesma stack costuma servir a mais de um
uso — o widget de atendimento no site, o resumidor de e-mail, o classificador
de ticket — e a única forma de variar a instrução entre eles era o cliente
mandar um `system` no corpo de cada request, espalhando configuração por todo
código que chama a API.

O system prompt por chave (migration 0053) move essa variação pra credencial:
a chave carrega a instrução, e a request volta a ser só `{"model", "messages"}`.

## Precedência (a ordem é o contrato)

  1. `system` no corpo da request — quem manda instrução explícita continua
     mandando, exatamente como antes. É o que mantém a CLI fora disto: Claude
     Code, Codex e Cursor embutem o próprio system prompt, então a chave nunca
     compete com a ferramenta (ver validate_body em main.py, que é onde essa
     camada é aplicada).
  2. `system_prompt` da chave, se `use_custom_prompt` estiver ligado.
  3. `system_prompt` da stack.

Esta função resolve só (2) e (3): (1) mora em main.py, junto do resto do
tratamento de mensagens.

## Por que duas colunas, e por que texto vazio não conta

`use_custom_prompt` é o switch do painel e `system_prompt` é o rascunho —
desligar o prompt próprio não pode apagar o texto que o cliente escreveu.
A consequência é que os dois campos podem discordar: switch ligado com texto
vazio (o cliente ligou e ainda não escreveu, ou apagou tudo). Nesse caso vale
o da stack, nunca "nenhum prompt" — mesmo critério do `client_system_text` em
validate_body: instrução vazia não é instrução, e não deve apagar a
configuração da conta em silêncio.

## As rotas de imagem entram na MESMA precedência

`/v1/images/generations` e `/v1/images/edits` (difusão) seguem a regra acima
sem exceção: `prompt` no corpo ganha, depois o da chave, depois o da stack. O
que muda é só ONDE a regra é aplicada, e a assimetria vem de uma que já existia:

  - `generations` é JSON, e o gateway já materializa e reescreve o corpo para o
    `pin_model`. A resolução acontece ali (resolve_image_prompt), e o pod recebe
    um corpo que já tem o prompt final — funciona com qualquer versão da imagem
    do pod, inclusive a que estiver rodando antes deste deploy.
  - `edits` é multipart repassado em STREAMING e o gateway nunca o parseia (é o
    que impede 21 MiB de referências virarem RAM numa réplica única
    compartilhada por todos os tenants). Sem parsear, o gateway não tem como
    saber se o cliente mandou `prompt` — então ele não decide nada: manda o
    prompt configurado no header IMAGE_PROMPT_HEADER e quem resolve a
    precedência é o pod, que já tem o form parseado em mãos.

A consequência prática dessa segunda metade é de ORDEM DE DEPLOY: um pod com
imagem anterior a este contrato ignora o header, e uma chave que dependa do
prompt configurado leva `400 missing_prompt` no `edits` até o pod ser recriado.
O `generations` não tem essa janela.

Módulo puro (sem I/O) como usage_class.py, client_identity.py e cli_policy.py:
resolver o prompt é função dos dois dicts, e quem faz o I/O de achar a stack da
chave (`resolve_key_stack`) é o main.py.

## Propagação

O dict `entry` vem do key_cache (TTL de KEY_CACHE_TTL_S, 60s por default), que
guarda a linha inteira da chave. Editar o prompt no painel do cliente escreve
direto no Supabase, sem passar por este gateway — então a mudança vale a partir
do próximo miss de cache, e não instantaneamente. É o mesmo atraso que já vale
pra expiração e pro status da chave.
"""

import base64
from typing import Any

# Header que leva o prompt configurado ao pod de difusão em /v1/images/edits.
# O sufixo anuncia o encoding porque o valor é texto livre do cliente: prompt
# com acento, emoji ou quebra de linha não cabe cru num header HTTP (latin-1,
# uma linha só), e um encoding implícito aqui viraria UnicodeEncodeError no
# httpx — falha no gateway, para um texto que o cliente digitou no painel.
#
# O OUTRO LADO é `PROMPT_HEADER` em docker/image/policy.py, e os dois nomes
# precisam bater. Não dá para importar um do outro: gateway e pod são imagens
# Docker separadas, sem código em comum. Mudar aqui sem mudar lá não quebra
# teste nenhum — só faz o prompt configurado parar de chegar, em silêncio.
IMAGE_PROMPT_HEADER = "X-Default-Prompt-B64"

# Teto do texto que viaja no header, ANTES do base64. Existe porque
# api_keys.system_prompt é `text` sem limite e o servidor do pod tem teto por
# linha de header (uvicorn/h11): um prompt de dezenas de KB não viraria um erro
# legível, viraria uma requisição malformada no meio do upload. 4 KiB é ~8x o
# que o pipeline chega a ler (IMAGE_MAX_SEQUENCE_LENGTH=512 tokens), então o
# corte nunca alcança texto que teria efeito na imagem.
MAX_PROMPT_HEADER_BYTES = 4096


def resolve_system_prompt(entry: dict, stack: dict | None) -> str | None:
    """Texto do system prompt que vale pra esta chave, ou None se não há nenhum.

    `entry` é a linha de api_keys (find_active_key), `stack` é a stack já
    resolvida por resolve_key_stack — passada de fora, e não re-resolvida aqui,
    pra manter o módulo puro e porque todo call site já tem a stack em mãos."""
    if entry.get("use_custom_prompt"):
        own = (entry.get("system_prompt") or "").strip()
        if own:
            return own
    return ((stack or {}).get("system_prompt") or "").strip() or None


def resolve_image_prompt(
    client_prompt: Any, entry: dict, stack: dict | None
) -> str | None:
    """Prompt efetivo de uma request de difusão, ou None se não há nenhum.

    Mesma precedência do chat, com o `prompt` do corpo no papel do `system`:
    texto do cliente ganha, senão o da chave, senão o da stack. `None` aqui não
    é erro — é "nem o cliente nem a configuração deram um prompt", e quem
    responde 400 continua sendo o pod (`missing_prompt`), que é quem valida o
    resto do corpo.

    Prompt do cliente é devolvido CRU, sem strip: espaço em branco não muda a
    imagem, e reescrever o texto de quem mandou um explicitamente colocaria o
    gateway no meio de um campo que é só do cliente. O `.strip()` aqui decide
    apenas se o texto CONTA — mesma regra do client_system_text em
    validate_body: instrução vazia não é instrução e não pode apagar em
    silêncio o que a conta configurou."""
    if isinstance(client_prompt, str) and client_prompt.strip():
        return client_prompt
    return resolve_system_prompt(entry, stack)


def encode_prompt_header(text: str | None) -> str | None:
    """Valor do IMAGE_PROMPT_HEADER para `text`, ou None quando não há prompt.

    Trunca em MAX_PROMPT_HEADER_BYTES. O corte é em bytes e pode cair no meio de
    um caractere multibyte, então o `decode(errors="ignore")` derruba o pedaço
    órfão — sem ele o header carregaria UTF-8 inválido e o pod descartaria o
    prompt INTEIRO por causa do último caractere."""
    if not text:
        return None
    raw = text.encode("utf-8")[:MAX_PROMPT_HEADER_BYTES]
    raw = raw.decode("utf-8", "ignore").encode("utf-8")
    if not raw:
        return None
    return base64.b64encode(raw).decode("ascii")
