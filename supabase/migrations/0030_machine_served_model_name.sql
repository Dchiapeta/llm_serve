-- Nome com que o vLLM SERVE o modelo (o alias de --served-model-name), separado
-- do machines.model_name (que é o path do HF, usado como --model no boot).
--
-- Bug corrigido: o gateway fixa o campo "model" das requisições de MODELO BASE
-- em machines.model_name (o path do HF, ex.: "Qwen/Qwen3.6-27B-FP8"). Mas quando
-- o template passa "--served-model-name pro-base", o vLLM só atende pelo alias
-- ("pro-base") e rejeita o path do HF com 404 "model does not exist". O painel já
-- resolvia o alias (lib/machines.ts:parseServedModelName); o gateway não.
--
-- Aqui guardamos o alias na própria máquina (o gateway lê tudo de `machines`, sem
-- buscar template a cada request) e o gateway passa a fixar served_model_name.
-- NULL = template sem --served-model-name → o gateway cai no fallback model_name
-- (que, nesse caso, é o próprio nome servido pelo vLLM).

alter table machines add column if not exists served_model_name text;

-- Backfill das máquinas existentes: extrai o alias do env do template
-- (VLLM_EXTRA_ARGS) ou do start_command. Regex POSIX: após a flag, um ou mais
-- separadores (espaço ou '='), captura o primeiro token não-espaço (o nome
-- canônico; --served-model-name aceita vários, o primeiro é o oficial).
update machines m
set served_model_name = coalesce(
  substring(t.env->>'VLLM_EXTRA_ARGS' from '--served-model-name[[:space:]=]+([^[:space:]]+)'),
  substring(t.start_command from '--served-model-name[[:space:]=]+([^[:space:]]+)')
)
from templates t
where m.template_id = t.id
  and m.served_model_name is null
  and (
    t.env->>'VLLM_EXTRA_ARGS' ~ '--served-model-name'
    or t.start_command ~ '--served-model-name'
  );
