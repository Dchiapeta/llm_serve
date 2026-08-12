"""Qual system prompt vale pra uma request — o da STACK ou o da CHAVE.

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
