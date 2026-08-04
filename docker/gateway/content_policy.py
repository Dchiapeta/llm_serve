"""Política de conteúdo: nunca repassar ao pod algo que ele vai rejeitar.

Funções PURAS, sem env/rede/FastAPI — importáveis pelos testes sem as env vars
obrigatórias do main.py (mesma disciplina de context_budget.py).

---------------------------------------------------------------------------
O bug que este módulo existe pra impedir
---------------------------------------------------------------------------
Um cliente agêntico reenvia a conversa INTEIRA a cada turno. Então um bloco de
conteúdo que o pod rejeita com 400 não estraga uma requisição: estraga a
SESSÃO. O bloco fica no histórico, volta no turno seguinte, e o pod rejeita de
novo — indefinidamente.

Observado em produção (29/07/2026, Pro): um screenshot colado no Claude Code
levou `400 At most 0 image(s) may be provided in one prompt`, e depois disso
até um "oi" recebia o mesmo 400. Não havia recuperação além de `/clear`, e o
usuário não tem como saber disso.

Por isso a regra aqui é recortar o excedente e DEIXAR PASSAR, em vez de
propagar o erro do vLLM. Uma resposta degradada ("não consegui ver 2 das
imagens") mantém a conversa viva; um 400 a mata.

Isto é REDE DE PROTEÇÃO, não a solução do caso normal: os planos
compartilhados aceitam imagem (--limit-mm-per-prompt no template), e este
caminho só dispara acima do limite configurado.
"""

# Aviso que substitui a mídia recortada. Vai como texto no prompt, então o
# modelo LÊ isso e consegue explicar ao usuário em vez de alucinar sobre uma
# imagem que nunca viu. Em português como o resto das mensagens de erro
# client-facing do gateway (ver context_budget.ContextWindowExceeded).
MEDIA_DROPPED_NOTE = (
    "[{n} imagem(ns) removida(s) pelo servidor: o limite deste plano é "
    "{limit} por mensagem — descreva o conteúdo em texto ou envie menos "
    "imagens]"
)

# Tipos de parte que contam como imagem. Dois formatos convivem:
#   chat/completions -> {"type": "image_url", "image_url": {...}}
#   Responses API    -> {"type": "input_image", "image_url": "..."}
# O conversor da Anthropic (anthropic_compat._user_content_to_openai_messages)
# normaliza bloco `image` base64 pra "image_url", então /v1/messages cai no
# primeiro formato.
IMAGE_PART_TYPES = frozenset({"image_url", "input_image"})


def text_of(content) -> str:
    """Texto de um `content` de mensagem, aceitando os DOIS formatos que o
    protocolo OpenAI permite: string crua ou lista de partes tipadas
    ([{"type": "text", "text": ...}, ...]).

    Existe por causa de um bug de descarte silencioso no system prompt: o
    caminho do chat só considerava `content` quando era `str`, então um
    system em formato de lista — protocolo válido, e cada vez mais comum —
    era jogado fora. E, como o cliente ainda assim "tinha mandado um system",
    o fallback do prompt da stack também não rodava: a request seguia SEM
    instrução nenhuma, nem a do cliente nem a da plataforma.

    Partes não-texto (imagem, áudio) são ignoradas de propósito: quem chama
    isto quer instrução, e uma imagem no system não vira texto."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def count_images(messages: list) -> int:
    """Quantas partes de imagem existem no corpo, somando todas as mensagens."""
    total = 0
    for m in messages if isinstance(messages, list) else []:
        if isinstance(m, dict) and isinstance(m.get("content"), list):
            total += sum(
                1
                for p in m["content"]
                if isinstance(p, dict) and p.get("type") in IMAGE_PART_TYPES
            )
    return total


def clamp_media(
    messages: list, max_images: int | None, text_part_type: str = "text"
) -> tuple[list, int]:
    """Recorta imagens além de `max_images`, devolvendo (messages, removidas).

    `text_part_type` é o tipo da parte de texto no formato em uso: "text" em
    chat/completions (e portanto em /v1/messages, que é convertido pra lá) e
    "input_text" na Responses API do Codex. Errar isso faria o vLLM rejeitar a
    parte do aviso — o oposto do objetivo.

    Conta da ÚLTIMA mensagem pra trás: numa conversa longa, a imagem que o
    usuário acabou de mandar é a que ele quer que o modelo veja. Manter as
    primeiras (mais antigas) e descartar a nova seria o oposto do esperado.

    `max_images=None` (máquina sem a coluna preenchida, ex.: pod anterior à
    migration) ⇒ no-op. Fail-open de propósito: sem saber o limite do pod, o
    palpite errado ou barra imagem que funcionaria, ou deixa passar o que não
    funciona — e o comportamento de hoje já é deixar passar.

    Não muta a entrada: devolve mensagens novas só onde houve recorte, pra não
    surpreender chamador que ainda tenha referência ao dict original."""
    if not isinstance(messages, list) or max_images is None or max_images < 0:
        return messages, 0

    total = count_images(messages)
    if total <= max_images:
        return messages, 0

    kept = 0
    dropped = 0
    out: list = []
    # Varre de trás pra frente PRESERVANDO as primeiras `max_images` que
    # encontrar — ou seja, as mais RECENTES. Descartar a partir do fim seria o
    # oposto: manteria a imagem velha e jogaria fora a que o usuário acabou de
    # colar, que é justamente a que ele quer que o modelo veja.
    for m in reversed(messages):
        if isinstance(m, dict) and isinstance(m.get("content"), list):
            new_parts = []
            for p in reversed(m["content"]):
                if isinstance(p, dict) and p.get("type") in IMAGE_PART_TYPES:
                    if kept < max_images:
                        kept += 1
                    else:
                        dropped += 1
                        continue
                new_parts.append(p)
            new_parts.reverse()
            m = {**m, "content": new_parts}
        out.append(m)
    out.reverse()

    if dropped:
        _append_note(out, dropped, max_images, text_part_type)
    return out, dropped


def _append_note(
    messages: list, dropped: int, limit: int, text_part_type: str
) -> None:
    """Anexa o aviso na última mensagem de user (é onde o modelo vai olhar).

    Muta `messages` in-place — só é chamada com a lista nova de clamp_media."""
    note = MEDIA_DROPPED_NOTE.format(n=dropped, limit=limit)
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, list):
            messages[i] = {
                **m,
                "content": content + [{"type": text_part_type, "text": note}],
            }
        elif isinstance(content, str):
            messages[i] = {**m, "content": f"{content}\n\n{note}"}
        else:
            continue
        return
    # nenhuma mensagem de user com content aproveitável: melhor perder o aviso
    # do que inventar uma mensagem e mudar a estrutura da conversa
