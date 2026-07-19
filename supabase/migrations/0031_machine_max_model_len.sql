-- Janela de contexto (--max-model-len) da máquina, para o gateway conhecer o
-- limite real do vLLM que vai atender a request.
--
-- Bug corrigido: o gateway aplicava só piso/teto fixos em max_tokens
-- (MIN_MAX_TOKENS/MAX_MAX_TOKENS) sem saber a janela do modelo — um cliente
-- com prompt grande (ex.: Claude Code, ~26k tokens só de system+tools) recebia
-- o 400 cru do vLLM ("maximum context length is 16384 tokens..."). Com a
-- janela na própria máquina (mesmo padrão de served_model_name/0030: o gateway
-- lê tudo de `machines`, sem buscar template a cada request), o gateway pode
-- clampar max_tokens ao orçamento restante e devolver erro claro quando nem o
-- prompt cabe. NULL = template sem --max-model-len → sem clamp (o vLLM usa a
-- janela nativa do config do modelo, que não temos como conhecer aqui).

alter table machines add column if not exists max_model_len integer;

-- Backfill das máquinas existentes: extrai a janela do env do template
-- (VLLM_EXTRA_ARGS) ou do start_command. Mesma regex POSIX da 0030: após a
-- flag, um ou mais separadores (espaço ou '='), captura os dígitos.
update machines m
set max_model_len = coalesce(
  substring(t.env->>'VLLM_EXTRA_ARGS' from '--max-model-len[[:space:]=]+([0-9]+)'),
  substring(t.start_command from '--max-model-len[[:space:]=]+([0-9]+)')
)::integer
from templates t
where m.template_id = t.id
  and m.max_model_len is null
  and (
    t.env->>'VLLM_EXTRA_ARGS' ~ '--max-model-len'
    or t.start_command ~ '--max-model-len'
  );
