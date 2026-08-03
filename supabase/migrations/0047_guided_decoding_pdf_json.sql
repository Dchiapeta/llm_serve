-- Liga guided decoding (outlines) no Pro e no VibeCoder, para validar o caso
-- de uso "extração estruturada de documento (PDF -> JSON)".
--
-- O que isso resolve: hoje um cliente pode até pedir JSON via prompt, mas
-- sem garantia estrutural. Com --guided-decoding-backend habilitado, o vLLM
-- aceita response_format: {"type": "json_schema", ...} (ou guided_json) e
-- FORÇA a geração a seguir o schema token a token. O gateway já repassa esses
-- campos sem tocar (docker/gateway/main.py:validate_body/proxy só normaliza
-- model/max_tokens/sampling params) — nenhuma mudança de código é necessária,
-- só ligar o backend no boot do vLLM.
--
-- outlines em vez de xgrammar: é o backend mais compatível como ponto de
-- partida na versão pinada do vLLM (vllm/vllm-openai:v0.24.0, docker/Dockerfile).
-- xgrammar fica como alternativa a testar depois se precisar de mais
-- performance.
--
-- Risco para quem já usa a stack hoje: baixo. A flag só adiciona uma
-- capacidade nova (aceitar response_format/guided_json) — quem não manda
-- esses campos não deveria notar diferença de comportamento.
--
-- Max fica de fora por ora: nenhuma demanda reportada, e o caso de uso está
-- sendo validado primeiro no Pro/VibeCoder.
--
-- Como sempre: o env de um pod é lido de templates.env NO MOMENTO DA CRIAÇÃO
-- (lib/actions.ts:createPodInput) — só vale para pods RECRIADOS depois desta
-- migration.
--
-- Idempotente: o !~ evita duplicar a flag se a migration rodar duas vezes.
update templates
set env = env || jsonb_build_object(
      'VLLM_EXTRA_ARGS',
      trim(coalesce(env->>'VLLM_EXTRA_ARGS', '')) || ' --guided-decoding-backend outlines'
    )
where plan in ('Pro', 'VibeCoder')
  and coalesce(env->>'VLLM_EXTRA_ARGS', '') !~ '--guided-decoding-backend';
