-- Liga prefix caching isolado por cache_salt no plano Pro.
--
-- Mesmas duas edições que a 0040 fez no VibeCoder, agora que o resultado lá foi
-- validado em produção: os turnos do Claude Code saíram de ~129s para 3-7s.
--
-- NENHUMA mudança de código é necessária. A imagem (docker/agent/proxy_policy.py
-- + o --enable-prefix-caching explícito no entrypoint) e o gateway (plumbing do
-- stack_id até o agent) já estão no ar desde o rollout do VibeCoder. Ligar um
-- plano novo ser só config de template é exatamente o que o contrato de duas
-- variáveis do docker/entrypoint.sh existe pra permitir.
--
-- Por que vale o mesmo raciocínio do VibeCoder: do ponto de vista do vLLM é o
-- MESMO modelo. Qwen/Qwen3.6-27B-FP8 e Qwen/Qwen3.5-9B declaram os dois
-- `architectures: ["Qwen3_5ForConditionalGeneration"]` e `model_type: qwen3_5`
-- no config.json do HF. Logo: híbrido (linear + full attention), então
-- is_prefix_caching_supported devolve False por padrão e o
-- --enable-prefix-caching EXPLÍCITO é obrigatório — só remover o --no- deixaria
-- o caching desligado em silêncio. E mamba_cache_mode cai em 'align'
-- (page_size × 2 por sequência, barato), nunca em 'all'.
--
-- Duas diferenças do Pro pra observar no log de boot, nenhuma bloqueante:
--
--   1. TP=2 (gpu_count 2, PCIe sem NVLink). O caching não interage com tensor
--      parallelism, mas o pod depende de NCCL_P2P_DISABLE=1 +
--      --disable-custom-all-reduce pra não deadlockar no boot.
--
--   2. O vLLM força o attention block size a igualar a mamba page size (no
--      VibeCoder ficou em 1056 tokens). No 27B o estado mamba é maior, então o
--      bloco tende a ser maior — isso grosseiriza a granularidade de reuso e
--      muda o "GPU KV cache size: N tokens". Comparar esse N com o do pod
--      anterior é a verificação de capacidade.
--
-- O que este fix NÃO resolve: o gargalo de decode da 2× A40. A A40 é compute
-- capability 8.6 e o corte de FP8 nativo do vLLM é 89
-- (CudaPlatform.supports_fp8 == has_device_capability(89)), então os pesos FP8
-- passam pelo caminho Marlin de desempacotamento — economia de VRAM sim,
-- speedup de compute não. Somado ao all-reduce via RAM do host (efeito
-- permanente do NCCL_P2P_DISABLE), sobra um custo por token gerado que só troca
-- de GPU resolve. O caching ataca o PREFILL; isso é DECODE. Medir com
-- "Avg generation throughput: N tokens/s" no log depois de subir.
--
-- ATENÇÃO: o env de um pod é lido de templates.env NO MOMENTO DA CRIAÇÃO
-- (lib/actions.ts:createPodInput passa imageName+env explícitos, sem
-- templateId). Só vale para pods RECRIADOS depois desta migration — despausar
-- mantém o env antigo.

update templates
set env = coalesce(env, '{}'::jsonb)
          || jsonb_build_object('PREFIX_CACHE_ISOLATION', 'cache_salt')
where plan = 'Pro';

-- Ground truth do hit rate: sem esta flag o vLLM não popula
-- usage.prompt_tokens_details.cached_tokens, e "0% de cache" fica
-- indistinguível de "a feature não ligou" — o teste deixa de ser falseável. O
-- agent lê esse campo pra métrica por chave (tokens_cached) e o
-- scripts/loadtest.py pro relatório de TTFT/cache.
--
-- Idempotente pelo ~. NÃO adicionar --mamba-block-size: com prefix caching
-- ligado o vLLM já alinha sozinho ao block_size, e a flag num pod SEM caching é
-- erro fatal de boot (vllm/config/vllm.py:validate_mamba_block_size).
update templates
set env = env || jsonb_build_object(
      'VLLM_EXTRA_ARGS',
      trim(coalesce(env->>'VLLM_EXTRA_ARGS', '')) || ' --enable-prompt-tokens-details'
    )
where plan = 'Pro'
  and coalesce(env->>'VLLM_EXTRA_ARGS', '') !~ '--enable-prompt-tokens-details';
