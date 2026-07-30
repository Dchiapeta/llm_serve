-- Imagens funcionando no VibeCoder e no Pro + teto conhecido pelo gateway.
--
-- ---------------------------------------------------------------------------
-- Contexto: o bug que motivou isto
-- ---------------------------------------------------------------------------
-- Screenshot colado no Claude Code apontado pro Pro devolveu
-- "400 At most 0 image(s) may be provided in one prompt" — e DEPOIS DISSO toda
-- mensagem da conversa deu o mesmo 400, inclusive um "oi". Registrado em
-- gateway_requests: 200 às 03:51:21, e 400 às 03:51:41, 03:52:46 e 03:52:51.
--
-- Duas causas separadas:
--
--   A. O template do Pro tinha --limit-mm-per-prompt {"image":0,"video":0}, e
--      limite 0 em TODAS as modalidades faz o vLLM rodar em text-only mode
--      (vllm/multimodal/registry.py) — imagem é rejeitada de saída.
--
--   B. Cliente agêntico reenvia a conversa INTEIRA a cada turno, então o bloco
--      de imagem rejeitado volta no turno seguinte e é rejeitado de novo, pra
--      sempre. Um 400 não estraga uma requisição, estraga a SESSÃO — e o
--      usuário não tem como saber, nem como sair sem /clear.
--
-- Esta migration resolve (A) e dá ao gateway o dado que ele precisa pra
-- resolver (B) recortando o excedente (docker/gateway/content_policy.py).

-- ---------------------------------------------------------------------------
-- 1. machines.max_images_per_prompt — ANTES de mexer nos templates
-- ---------------------------------------------------------------------------
-- ORDEM IMPORTA e é contraintuitiva: o backfill tem que ler o env ANTIGO do
-- template, porque um pod em execução carrega o env do momento em que foi
-- CRIADO (lib/actions.ts:createPodInput). Se o backfill rodasse depois do
-- passo 2, uma máquina Pro viva receberia 4 enquanto o pod dela ainda está em
-- text-only mode — o gateway deixaria 4 imagens passarem, o pod devolveria 400
-- e o envenenamento de sessão voltaria, agora causado pela própria migration.
--
-- Rodando antes, cada máquina viva recebe o teto REAL do pod dela (Pro→0,
-- VibeCoder→999) e ganha a proteção de recorte imediatamente, sem esperar
-- recreate. Na recriação o valor certo vem do parseImageLimit.
--
-- Mesmo padrão de max_model_len (0031) e max_concurrent_seqs (0039): extraído
-- do template por lib/machines.ts:parseImageLimit na criação da máquina.
-- supa.get_machine usa select=*, então a coluna chega ao gateway sem mudança.
alter table machines add column if not exists max_images_per_prompt integer;

comment on column machines.max_images_per_prompt is
  'Teto de imagens por prompt do vLLM neste pod (--limit-mm-per-prompt), '
  'extraído do template na criação (lib/machines.ts:parseImageLimit). O '
  'gateway recorta imagem acima disso e avisa no prompt, em vez de deixar o '
  'pod devolver 400 — que num cliente que reenvia a conversa inteira '
  'envenenaria a sessão. NULL = desconhecido, e aí o gateway não recorta nada '
  '(fail-open, comportamento anterior a esta migration).';

-- 999 quando o template não tem a flag: é o default do vLLM
-- (config/multimodal.py), e registrar isso é diferente de NULL ("não sei") —
-- com 999 o gateway sabe que imagem é permitida e não precisa recortar.
update machines m
set max_images_per_prompt = case
      when coalesce(t.env->>'VLLM_EXTRA_ARGS', '') !~ '--limit-mm-per-prompt' then 999
      else nullif(
        substring(
          t.env->>'VLLM_EXTRA_ARGS' from '"image"[: ]*([0-9]+)'
        ), ''
      )::integer
    end
from templates t
where t.id = m.template_id
  and m.status <> 'terminated'
  and m.max_images_per_prompt is null;

-- ---------------------------------------------------------------------------
-- 2. image:4, video:0 nos dois planos compartilhados
-- ---------------------------------------------------------------------------
-- Pro sai de image:0 (text-only) e VibeCoder sai do default IMPLÍCITO de 999
-- por modalidade — os dois passam a ter a MESMA regra explícita.
--
-- Por que não simplesmente remover a flag do Pro:
--   * video:0 fica. Vídeo não tem uso em cliente de código, e o profiling de
--     vídeo reserva muito mais memória que o de imagem (frames × resolução).
--     Mantendo em 0 preservamos quase toda a economia de VRAM sem custar a
--     funcionalidade que o usuário quer.
--   * image:4 em vez de 999. Cobre com folga colar screenshots num turno, e um
--     teto BAIXO é justamente o que transforma "sessão morta" em "aviso de 2
--     imagens recortadas" quando o cliente exagera.
--
-- Max fica de fora: pod dedicado, janela de 16k, e ninguém reportou querer
-- imagem lá. Mexer sem demanda só custaria KV.
--
-- Idempotente: o regexp_replace só age se o valor atual for diferente, e o
-- WHERE evita reescrever quem já está no formato final.
update templates
set env = env || jsonb_build_object(
      'VLLM_EXTRA_ARGS',
      case
        when env->>'VLLM_EXTRA_ARGS' ~ '--limit-mm-per-prompt' then
          regexp_replace(
            env->>'VLLM_EXTRA_ARGS',
            '--limit-mm-per-prompt[= ]+\S+',
            '--limit-mm-per-prompt {"image":4,"video":0}'
          )
        else
          trim(coalesce(env->>'VLLM_EXTRA_ARGS', ''))
          || ' --limit-mm-per-prompt {"image":4,"video":0}'
      end
    )
where plan in ('VibeCoder', 'Pro')
  and coalesce(env->>'VLLM_EXTRA_ARGS', '') !~
      '--limit-mm-per-prompt \{"image":4,"video":0\}';
