-- Liga prefix caching no VibeCoder, isolado por cache_salt.
--
-- O problema que isto resolve: cliente agêntico (Claude Code) manda ~85k
-- tokens por turno, e quase tudo é o turno anterior + um delta. Sem caching,
-- o vLLM reprocessa o prompt INTEIRO a cada mensagem — é a maior fonte de
-- lentidão do plano, na ordem de dezenas de segundos de prefill por turno.
--
-- Por que estava desligado: pod compartilhado entre tenants. Um cache hit de
-- prefixo reduz o TTFT de forma observável, então um tenant mede o próprio
-- tempo e infere se um prefixo já foi processado por outro. Canal lateral.
--
-- O que muda: o agent passa a injetar um `cache_salt` derivado do stack_id em
-- toda request generativa (docker/agent/proxy_policy.py). O salt entra no hash
-- do primeiro bloco de KV e encadeia — stacks diferentes nunca colidem, a
-- mesma stack segue reaproveitando o próprio prefixo entre turnos.
--
-- PRÉ-REQUISITO DE IMAGEM: só tem efeito com a imagem que contém
-- proxy_policy.py. Numa imagem antiga o shell ignora a variável e o pod sobe
-- lento e seguro, sem caching — nenhuma ordem de deploy consegue ligar o
-- caching sem o isolamento (o contrato está documentado em
-- docker/entrypoint.sh).
--
-- E, como sempre, o env de um pod é lido de templates.env NO MOMENTO DA
-- CRIAÇÃO: só vale para pods RECRIADOS depois desta migration.
--
-- Pro fica de fora POR ORA de propósito: recebe as mesmas duas edições depois
-- que o VibeCoder validar A/B/C/D, e aí sem deploy novo.

update templates
set env = coalesce(env, '{}'::jsonb)
          || jsonb_build_object('PREFIX_CACHE_ISOLATION', 'cache_salt')
where name = 'VibeCoder_A40_Qwen3.5';

-- --enable-prompt-tokens-details: sem ele o vLLM não popula
-- usage.prompt_tokens_details.cached_tokens, e aí "0 tokens de cache" fica
-- indistinguível de "a feature não ligou". É o ground truth da verificação —
-- sem isso o rollout não é falseável.
--
-- Idempotente: o ~ evita duplicar a flag se a migration rodar duas vezes.
-- NÃO adicionar --mamba-block-size: com prefix caching ligado o vLLM já alinha
-- sozinho ao block_size, e a flag num pod SEM caching é erro fatal de boot
-- (vllm/config/vllm.py:validate_mamba_block_size).
update templates
set env = env || jsonb_build_object(
      'VLLM_EXTRA_ARGS',
      trim(coalesce(env->>'VLLM_EXTRA_ARGS', '')) || ' --enable-prompt-tokens-details'
    )
where name = 'VibeCoder_A40_Qwen3.5'
  and coalesce(env->>'VLLM_EXTRA_ARGS', '') !~ '--enable-prompt-tokens-details';
