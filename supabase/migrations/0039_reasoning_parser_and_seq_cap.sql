-- Liga o reasoning-parser nativo nos planos compartilhados + fecha o buraco
-- de machines.max_concurrent_seqs.
--
-- ---------------------------------------------------------------------------
-- 1. ENABLE_REASONING_PARSER nos templates VibeCoder e Pro
-- ---------------------------------------------------------------------------
-- Os dois templates já traziam REASONING_PARSER='qwen3', mas o entrypoint
-- (docker/entrypoint.sh) só emite --reasoning-parser quando ENABLE_REASONING_
-- PARSER='true' — que NUNCA foi setada. Resultado: o vLLM subia sem o parser,
-- o raciocínio do modelo saía dentro de "content", e o gateway (main.py,
-- REASONING_LEAK_PLANS) segurava o stream INTEIRO até achar um </think>.
-- Como o piso de max_tokens é 8000, o cliente ficava olhando pra uma tela
-- parada durante todo o "pensamento" do modelo, em toda requisição.
--
-- Com o parser ligado o vLLM separa o raciocínio em "reasoning_content", e os
-- dois filtros do gateway (chat/completions e /v1/messages) se desligam
-- sozinhos naquela resposta — o texto volta a sair token a token.
--
-- ATENÇÃO: o env de um pod é lido de templates.env NO MOMENTO DA CRIAÇÃO
-- (lib/actions.ts:createPodInput passa imageName+env explícitos, sem
-- templateId). Esta migration só tem efeito em pods RECRIADOS depois dela —
-- despausar um pod existente mantém o env antigo.

update templates
set env = coalesce(env, '{}'::jsonb)
          || jsonb_build_object('ENABLE_REASONING_PARSER', 'true')
where plan in ('VibeCoder', 'Pro')
  and env ? 'REASONING_PARSER';

-- ---------------------------------------------------------------------------
-- 2. Backfill de machines.max_concurrent_seqs
-- ---------------------------------------------------------------------------
-- A coluna existe desde a 0028 e o gateway a lê em machine_capacity(), mas
-- NENHUM produtor a escrevia — nem o painel, nem o gateway. Toda máquina caía
-- no DEFAULT_MAX_CONCURRENT_SEQS do gateway, ou seja: o --max-num-seqs real do
-- template nunca chegava a quem decide devolver 429. Hoje os dois coincidem
-- por acaso; deixaria de coincidir no instante em que um template mudasse a
-- flag, e a mudança seria silenciosamente ignorada.
--
-- O produtor novo é lib/machines.ts:parseMaxNumSeqs, chamado na criação da
-- máquina. Este backfill cobre as que já existem, sem esperar um recreate.
-- Mesma regex do TS; NULL continua sendo válido (= sem a flag no template,
-- e aí o fallback do gateway é o comportamento certo).

update machines m
set max_concurrent_seqs = nullif(
      substring(
        coalesce(t.env->>'VLLM_EXTRA_ARGS', '') || ' ' || coalesce(t.start_command, '')
        from '--max-num-seqs[[:space:]=]+([0-9]+)'
      ),
      ''
    )::integer
from templates t
where t.id = m.template_id
  and m.status <> 'terminated'
  and m.max_concurrent_seqs is null
  and coalesce(t.env->>'VLLM_EXTRA_ARGS', '') || ' ' || coalesce(t.start_command, '')
      ~ '--max-num-seqs[[:space:]=]+[0-9]+';

comment on column machines.max_concurrent_seqs is
  'Teto de sequências concorrentes do vLLM (--max-num-seqs) do deploy desta '
  'máquina, extraído do template na criação (lib/machines.ts:parseMaxNumSeqs). '
  'O gateway aplica o 429 por máquina a partir daqui (check_concurrency). '
  'NULL = template sem a flag → fallback DEFAULT_MAX_CONCURRENT_SEQS.';
