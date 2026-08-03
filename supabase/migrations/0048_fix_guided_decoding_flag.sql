-- Remove --guided-decoding-backend dos templates: a flag NÃO EXISTE no vLLM
-- 0.24.0 e impedia o pod de subir.
--
-- O que aconteceu: a migration 0047 acrescentou
-- "--guided-decoding-backend outlines" ao VLLM_EXTRA_ARGS do Pro e do
-- VibeCoder pra habilitar response_format/json_schema. A flag foi REMOVIDA da
-- CLI nessa versão — no v0.24.0 não há uma única ocorrência de "guided" em
-- vllm/engine/arg_utils.py; a configuração virou o grupo
-- --structured-outputs-config (vllm/config/structured_outputs.py).
--
-- Como VLLM_EXTRA_ARGS entra cru no argparse do vLLM
-- (docker/entrypoint.sh:121), argumento desconhecido é erro fatal: o processo
-- morre no boot e a máquina falha ao subir. Foi exatamente o sintoma
-- observado ao recriar a máquina depois da 0047.
--
-- Mesma classe de erro da remoção do --rope-scaling nesta mesma versão: flag
-- de CLI do vLLM não é contrato estável entre releases, e o entrypoint não
-- valida nada — quem valida é o argparse, no boot, quando já é tarde.
--
-- POR QUE SIMPLESMENTE REMOVER, e não traduzir para a flag nova:
-- structured outputs não precisa de flag nenhuma aqui. O default de
-- StructuredOutputsConfig.backend é "auto" (structured_outputs.py:21), e com
-- "auto" o vLLM escolhe o backend por request conforme o conteúdo. Ou seja: o
-- response_format {"type": "json_schema"} do /v1/documents/extract funciona
-- com o template LIMPO. A 0047 não habilitou uma capacidade que faltava — ela
-- tentou configurar explicitamente algo que já vem ligado, e quebrou o boot no
-- caminho.
--
-- Se algum dia for preciso FIXAR um backend (ex.: xgrammar por performance),
-- a forma correta nesta versão é --structured-outputs-config, nunca a flag
-- antiga. Valores aceitos: auto, xgrammar, guidance, outlines,
-- lm-format-enforcer.
--
-- Como sempre: o env de um pod é lido de templates.env NO MOMENTO DA CRIAÇÃO
-- (lib/actions.ts:createPodInput). Máquina que falhou por causa da 0047 precisa
-- ser RECRIADA depois desta migration — despausar mantém o env quebrado.
--
-- Idempotente: o regexp_replace não faz nada se a flag já não estiver lá.
update templates
set env = env || jsonb_build_object(
      'VLLM_EXTRA_ARGS',
      trim(regexp_replace(
        env->>'VLLM_EXTRA_ARGS',
        '\s*--guided-decoding-backend(\s+\S+)?',
        '',
        'g'
      ))
    )
where coalesce(env->>'VLLM_EXTRA_ARGS', '') ~ '--guided-decoding-backend';
