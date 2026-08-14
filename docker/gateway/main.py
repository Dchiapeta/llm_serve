"""
Gateway estável de inferência — o ÚNICO endpoint público que o cliente final
conhece. Resolve em qual máquina está o adapter LoRA da conta e faz o proxy
(incluindo streaming SSE) para o agent daquele pod. O cliente nunca sabe em
qual pod está.

Fluxo por request:
  1. Autentica a chave HEX (Bearer) contra api_keys no Supabase (cache TTL),
     junto com o plano e o system_prompt configurados da stack da chave.
  2. Resolve a rota (routing_state). Regra primária: machine_id definido →
     proxy direto, independente do status (em 'migrating' a origem segue
     servindo até o flip). Só espera quando não há máquina servindo. Sem
     adapter, o modelo base é stack-aware: serve na máquina do stack da
     conta; pausada → realocação automática (reponta stack + move chaves)
     pra outra running com vaga do MESMO plano, ou religa a própria; sem
     stack, fallback por plano — nunca cai no modelo base de outro plano.
  3. Sem rota: alocação placeholder (primeira máquina running com slot livre),
     claim atômico, upsert da chave no agent, load do adapter, proxy.
  4. Injeta no body (chat completions): system prompt da conta + top-k de
     contexto da base de conhecimento (RAG básico do Go, embeddings
     via OpenAI).
  5. Máquina fora do ar → 503 imediato, nunca pendura o request.
  6. Sem nenhuma máquina running com vaga → auto-wake: religa (startPod) um
     pod pausado do template do plano e responde 503 + Retry-After; o retry
     do cliente aloca normalmente quando o vLLM estiver de pé. Toda religada
     agenda o reenvio das chaves ao agent (que reinicia zerado) e o fluxo
     base ainda garante a chave via upsert lazy antes de cada proxy.

Limitação aceita (MVP): réplica ÚNICA. O contador in-flight e (na Fase 5) o
idle reaper vivem em memória do processo — múltiplas réplicas cortariam
streams durante migração. Ver README.md.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import jsonschema
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

import demo
import document_extract
import document_generate
from anthropic_compat import (
    anthropic_sse_from_openai_stream,
    anthropic_to_openai_request,
    openai_to_anthropic_response,
)
from cli_policy import CLI_POLICY_ENFORCE, cli_block_reason
from client_identity import (
    CLIENT_LIMIT_ENFORCE,
    CLIENT_TOUCH_THROTTLE_S,
    CLIENT_WINDOW_DAYS,
    client_cap,
    client_fingerprint,
    client_ip,
    network_bucket,
)
from content_policy import clamp_media, text_of
from context_budget import (
    CONTEXT_IMAGE_TOKENS,
    ContextWindowExceeded,
    EstimateKind,
    PromptBudget,
    RESERVED_OUTPUT_TOKENS as MIN_MAX_TOKENS,
    anthropic_error_body,
    apply_context_budget,
    count_images,
    error_body_for,
    estimate_prompt_tokens,
    prompt_text_for_tokenize,
)
from context_budget import resolve_est_tokens as _resolve_est_tokens
from key_prompt import resolve_system_prompt
from lifecycle import LifecycleManager, MigrationError
from recovery import is_no_gpu_error, lock_active, spawn_tracked
from usage_class import classify_stack
from usage_norm import SseUsageScanner, normalize_usage, usage_from_event
from routing import RoutingStore
from runpod_api import RunPodClient
from stream_watchdog import UpstreamStreamTimeout, aiter_bytes_watchdog
from supa import SupaClient

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
GATEWAY_ADMIN_SECRET = os.environ.get("GATEWAY_ADMIN_SECRET", "")
KEY_CACHE_TTL_S = float(os.environ.get("KEY_CACHE_TTL_S", "60"))
KEY_CACHE_NEGATIVE_TTL_S = 5.0
LORA_LOAD_TIMEOUT_S = float(os.environ.get("LORA_LOAD_TIMEOUT_S", "120"))
LORA_BUCKET = os.environ.get("LORA_BUCKET", "loras")
# embeddings do RAG (Go) — mesmo modelo/dimensão usado na indexação
# pelo painel (lib/actions.ts), senão a similaridade de cosseno não faz sentido
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
RAG_TOP_K = int(os.environ.get("RAG_TOP_K", "4"))
# teto de adapters por máquina usado na alocação placeholder; a Fase 6 troca
# isso pelo cálculo de capacidade por VRAM (machine_lora_slots)
MAX_LORAS_PER_MACHINE = int(os.environ.get("MAX_LORAS_PER_MACHINE", "8"))

# ---------- limites de abuso/custo por chave (produção multi-tenant) ----------
# rate limit (token bucket, req/min) por chave — sem isso uma chave vazada ou
# um cliente descontrolado consumia GPU sem nenhum teto. Réplica única do
# gateway (ver docstring do módulo), então estado em memória é seguro — mesmo
# padrão do key_cache/in_flight.
#
# Por plano, seguindo o mesmo padrão de document_extract.MAX_DOCUMENT_BYTES:
# Enterprise é negociado por contrato ("Custom" na página de preços) e o
# valor abaixo é só o ponto de partida até o contrato pedir mais.
RATE_LIMIT_RPM = {
    "Go": 60.0,
    "VibeCoder": 60.0,
    "Pro": 120.0,
    "Max": 300.0,
    "Enterprise": 600.0,
}
DEFAULT_RATE_LIMIT_RPM = float(os.environ.get("RATE_LIMIT_RPM", "60"))


def rate_limit_rpm(plan: str | None) -> float:
    return RATE_LIMIT_RPM.get(plan or "", DEFAULT_RATE_LIMIT_RPM)

# concorrência: ELÁSTICA por MÁQUINA, não um teto fixo por chave — uma stack
# sozinha no pod pode usar quase toda a capacidade; outras dividem o mesmo
# teto conforme aparecem (ver check_concurrency). DEFAULT_MAX_CONCURRENT_SEQS
# só vale quando machines.max_concurrent_seqs (migration 0028) não foi
# preenchido pro pod ainda — é o fallback conservador, não a capacidade real.
DEFAULT_MAX_CONCURRENT_SEQS = int(os.environ.get("DEFAULT_MAX_CONCURRENT_SEQS", "8"))
# pod compartilhado (SHARED_POD_PLANS) sempre reserva esse mínimo de vagas —
# garante que quem chegar depois de um tenant pesado nunca fica 100%
# bloqueado esperando, só entra numa fila menor. Pod dedicado não reserva
# nada: não há vizinho pra proteger.
MIN_RESERVED_SLOTS_SHARED_POD = int(os.environ.get("MIN_RESERVED_SLOTS_SHARED_POD", "2"))
# corpo/params da request — nenhum destes existia antes: sem teto, um
# cliente BYOE podia mandar prompt gigante sem limite de tamanho/mensagens.
#
# 8 MB e não 1 MB: um screenshot colado no Claude Code vira base64 de
# 300 KB-1,5 MB, mais ~100 KB do system+tools em JSON — com 1 MB o mesmo gesto
# funcionava umas vezes e dava 400 outras, que é o pior comportamento possível
# (e um 400 aqui envenena a sessão inteira, ver content_policy.py).
# O teto de corpo nunca foi a defesa real contra abuso: quem limita custo é
# RATE_LIMIT_RPM, a quota diária de tokens e check_concurrency. Este teto é
# contra corpo absurdo/malformado, e 8 MB continua cumprindo esse papel.
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(8_000_000)))
MAX_MESSAGES = int(os.environ.get("MAX_MESSAGES", "200"))

# teto de tempo da inferência de extração de documento (/v1/documents/extract).
# Não confundir com os 60s do proxy_client: lá o read mede o SILÊNCIO entre
# chunks de um stream; aqui a requisição é não-streaming, então o read tem que
# cobrir a geração inteira do JSON de uma vez.
DOCUMENT_UPSTREAM_TIMEOUT_S = float(os.environ.get("DOCUMENT_UPSTREAM_TIMEOUT_S", "240"))

# mesmo motivo do DOCUMENT_UPSTREAM_TIMEOUT_S acima, agora pro proxy genérico
# de /v1/messages: com "stream": false o vLLM não manda NENHUM byte até a
# geração inteira terminar, então os 60s do proxy_client (pensados pra medir
# silêncio ENTRE chunks de um stream) matam requests não-streaming saudáveis
# sempre que max_tokens for alto o bastante pra passar de 60s de geração —
# confirmado ao vivo num teste com max_tokens=8000: a máquina respondia ao
# /health em <1s, mas o proxy estourava aos 60s e devolvia "máquina
# indisponível" pra uma máquina saudável que só ainda estava gerando.
#
# 90s, não mais — o public_url da máquina passa pelo Cloudflare do próprio
# RunPod, que tem teto de proxy de ~100-120s (524 "A timeout occurred") e não
# é algo que a gente controla. Subir esse valor pra perto ou acima disso
# (tentamos 600s) só troca um erro claro nosso por um 524 em HTML do
# Cloudflare, sem ganhar nenhum segundo real de espera — confirmado ao vivo:
# com 600s aqui, a request morreu do mesmo jeito aos ~127s, só que com uma
# página de erro do RunPod no lugar da nossa mensagem. 90s deixa margem de
# segurança pra sempre sermos nós a responder primeiro.
MESSAGES_NONSTREAM_TIMEOUT_S = float(os.environ.get("MESSAGES_NONSTREAM_TIMEOUT_S", "90"))

# /v1/messages com "stream": true — DUAS janelas, porque medem coisas distintas
# que o read timeout único do httpx confundia (e o comentário antigo do
# upstream_timeout afirmava, errado, que os 60s do proxy_client mediam só
# silêncio entre chunks).
#
# TTFT = tempo até o PRIMEIRO chunk, que é o prefill. Ele cresce com o tamanho
# do prompt, então a request de prompt máximo — a compactação do Claude Code,
# que reenvia a transcrição inteira — é justamente a que mais demora. No Pro
# (2× A40 TP=2, 27B, dequant Marlin, PCIe sem NVLink) um turno de ~85k tokens
# custa ~72s só de prefill. Com 60s o gateway matava a compactação e devolvia
# um stream vazio "bem-sucedido". O loadtest já registrava ~85s de primeiro
# byte no Go, o template LEVE (scripts/loadtest.py).
#
# O prefix caching AMORTIZA esse custo (medido em 14/08/2026: prefill real de
# ~1.100-1.500 tok/s, e TTFT de 36s num prompt de 94k com cache parcial contra
# 63s sem cache nenhum) — o que ele NÃO faz é eliminar o pior caso, que é
# justamente a compactação: ela reescreve o início do contexto e invalida o
# cache inteiro. Ou seja, a request que mais precisa do teto alto continua
# sendo a que menos se beneficia do cache.
#
# (Um comentário anterior aqui dizia que o prefix caching estava desligado nos
# modelos híbridos. O vLLM desliga por DEFAULT neles, mas os templates ligam
# explicitamente via PREFIX_CACHE_ISOLATION=cache_salt, que o entrypoint.sh
# traduz em --enable-prefix-caching. Está ligado.)
#
# 90s, mesma disciplina do MESSAGES_NONSTREAM_TIMEOUT_S: o public_url do pod
# passa pelo Cloudflare do RunPod, que corta em ~100-127s e devolve 524 em HTML,
# e é melhor sermos NÓS a responder primeiro, com uma mensagem que explica o que
# houve. Subir acima do teto deles só troca um erro nosso por um erro deles (já
# testado ao vivo — ver o comentário de MESSAGES_NONSTREAM_TIMEOUT_S acima).
#
# Ou seja: 90s é o teto prático, não uma folga escolhida. Se o prefill não cabe
# aí, nenhum timeout resolve — o que encolhe o prefill é compactar antes, e é
# por isso que context_budget.auto_compact_window reserva margem. Calibrar com o
# TTFT p95 real (scripts/loadtest.py --context-tokens 96000) antes de mexer.
MESSAGES_STREAM_TTFT_TIMEOUT_S = float(os.environ.get("MESSAGES_STREAM_TTFT_TIMEOUT_S", "90"))
# silêncio ENTRE chunks depois que o vLLM já começou a mandar SSE: aí sim é
# falha de verdade (pod travado, conexão zumbi), e o prazo curto do fix
# ca23611 continua valendo.
MESSAGES_STREAM_IDLE_TIMEOUT_S = float(os.environ.get("MESSAGES_STREAM_IDLE_TIMEOUT_S", "60"))
# heartbeat SSE (evento "ping" da Messages API) enquanto se espera o upstream.
# Mantém a conexão viva através do Cloudflare durante um prefill longo e evita
# deixar o cliente no escuro quando o filtro de <think> represa a saída.
# 0 desliga.
ANTHROPIC_SSE_PING_INTERVAL_S = float(os.environ.get("ANTHROPIC_SSE_PING_INTERVAL_S", "15"))

# /v1/chat/completions (e o resto do proxy genérico /v1/{path}) com stream:true
# — as MESMAS duas janelas do MESSAGES_STREAM_* acima, pelo mesmo motivo e com
# os mesmos valores. O proxy genérico nunca passou `timeout` no build_request e
# herdava o read de 60s do proxy_client; como durante o prefill não flui byte
# nenhum, esse read media o PREFILL INTEIRO em vez de silêncio entre chunks.
#
# Load test de 14/08/2026 no Pro (2× A40, ~43k tokens de prompt por request):
# 18 de 36 requests morreram sem entregar um único token, todas entre 61,3s e
# 62,6s — e todas registradas como 200, porque o cabeçalho chega muito antes de
# o corpo morrer. A taxa acompanha a concorrência (25% em 4, 50% em 6, 62% em
# 8): sozinho o prefill de 43k leva ~25s, mas com requests disputando a GPU ele
# atravessa os 60s. O caminho /v1/messages não sofria disso porque já tinha
# ganhado as duas janelas — este bug era o mesmo, só que na porta ao lado.
#
# Os 90s são o mesmo teto prático do MESSAGES_STREAM_TTFT_TIMEOUT_S e pela mesma
# razão: o public_url do pod passa pelo Cloudflare do RunPod, que corta em
# ~100-127s com um 524 em HTML. Subir além disso só troca um erro nosso por um
# deles. Calibrar com o TTFT p95 real (scripts/loadtest.py) antes de mexer — e
# lendo o p95 com cuidado, porque enquanto o corte existir ele vem censurado
# (tudo acima do teto some da amostra em vez de virar número alto).
CHAT_STREAM_TTFT_TIMEOUT_S = float(os.environ.get("CHAT_STREAM_TTFT_TIMEOUT_S", "90"))
# silêncio ENTRE chunks depois que o upstream já começou a mandar: aí sim é
# falha de verdade (pod travado, conexão zumbi) e a janela curta continua valendo.
CHAT_STREAM_IDLE_TIMEOUT_S = float(os.environ.get("CHAT_STREAM_IDLE_TIMEOUT_S", "60"))

# quota diária de tokens por conta (controle de custo real — rate limit e
# concorrência limitam volume de requests, não o custo de cada uma). 0 =
# sem teto (default, por plano — só liga onde configurado). Lida de
# usage_metrics, populada pelo metrics_collection_loop abaixo; cache curto
# evita 1 round-trip ao Supabase por request na hot path.
DAILY_TOKEN_BUDGET = {
    "Go": int(os.environ.get("DAILY_TOKEN_BUDGET_GO", "0")),
    # TRANSIÇÃO: nome antigo do plano "Go", aceito até a migration 0049 rodar
    # em produção. Remover junto com as outras entradas "VibeCoder" daqui.
    "VibeCoder": int(os.environ.get("DAILY_TOKEN_BUDGET_GO", "0")),
    "Pro": int(os.environ.get("DAILY_TOKEN_BUDGET_PRO", "0")),
    "Max": int(os.environ.get("DAILY_TOKEN_BUDGET_MAX", "0")),
    "Enterprise": int(os.environ.get("DAILY_TOKEN_BUDGET_ENTERPRISE", "0")),
}
TOKEN_QUOTA_CACHE_TTL_S = float(os.environ.get("TOKEN_QUOTA_CACHE_TTL_S", "60"))

# corte por inadimplência (migration 0050): tolerância entre a assinatura
# entrar em atraso (stacks.past_due_since, gravado pelo trigger que projeta
# chargefy_subscriptions) e a chave parar de responder. A política mora aqui
# e não no banco de propósito — mudar o prazo é trocar esta constante, não
# reescrever linha nenhuma.
#
# Vale para os dois cenários de atraso: fatura mensal vencida e valor anual
# não pago depois do trial de 7 dias.
#
# `or` em vez do default de get(): uma variável DECLARADA E VAZIA é o estado
# natural de quem copiou o .env.example e não preencheu, e float("") levanta
# ValueError no import — o módulo nem carrega, o gateway não sobe, e todo o
# tráfego de inferência de todos os clientes cai. Um prazo de cobrança não
# configurado tem que degradar para o default, nunca para outage.
BILLING_GRACE_HOURS = float(os.environ.get("BILLING_GRACE_HOURS") or "72")
# usage_metrics antes só era populado quando um admin abria o painel
# (collectUsageMetrics em lib/metrics.ts, chamado só no carregamento da
# página) — inviável como base de uma quota real, já que uma conta gerava
# uso ilimitado entre duas visitas ao painel sem nenhum registro. Este loop
# espelha aquela coleta, mas roda sozinho no processo do gateway.
METRICS_COLLECTION_INTERVAL_S = float(os.environ.get("METRICS_COLLECTION_INTERVAL_S", "120"))

# classificação de stacks por padrão de consumo (usage_class, migration 0032):
# janela móvel de avaliação, mínimo de dias ativos pra classificar, cooldown
# entre mudanças (histerese — um dia atípico não muda classe) e cadência do
# loop. 6h de cadência é mais que suficiente pra um sinal que exige dias de
# uso sustentado.
USAGE_CLASS_WINDOW_DAYS = int(os.environ.get("USAGE_CLASS_WINDOW_DAYS", "14"))
USAGE_CLASS_MIN_ACTIVE_DAYS = int(os.environ.get("USAGE_CLASS_MIN_ACTIVE_DAYS", "5"))
USAGE_CLASS_COOLDOWN_DAYS = int(os.environ.get("USAGE_CLASS_COOLDOWN_DAYS", "7"))
USAGE_CLASS_INTERVAL_S = float(os.environ.get("USAGE_CLASS_INTERVAL_S", "21600"))
# Rebalanceamento do teto de heavy (migration 0037): quando não há destino
# running com vaga, a cascata despausa/cria uma máquina, que leva minutos pra
# ficar de pé. Este é o atraso até a nova passada que efetivamente usa essa
# máquina — sem ele o desbalanceamento esperaria o próximo ciclo de 6h.
HIGH_CAP_RETRY_DELAY_S = float(os.environ.get("HIGH_CAP_RETRY_DELAY_S", "180"))
# Teto de novas tentativas encadeadas: sem ele, um desbalanceamento que nunca
# consegue destino (auto-provisionamento desligado, plano sem máquina livre)
# reagendaria a si mesmo pra sempre a cada 3 min. Esgotado o orçamento, o
# desbalanceamento espera o próximo ciclo de classificação.
HIGH_CAP_MAX_RETRIES = int(os.environ.get("HIGH_CAP_MAX_RETRIES", "3"))

# quanto tempo um request espera por um load em andamento de outro request
LOAD_WAIT_TIMEOUT_S = float(os.environ.get("LOAD_WAIT_TIMEOUT_S", "20"))
TOUCH_THROTTLE_S = 15.0
# lifecycle: liberação do slot por ociosidade (0 = desligado) e drain da
# migração. IDLE_RELEASE_MINUTES substitui IDLE_UNLOAD_MINUTES (fallback
# mantido pra não quebrar deploy existente).
IDLE_RELEASE_MINUTES = float(
    os.environ.get("IDLE_RELEASE_MINUTES", os.environ.get("IDLE_UNLOAD_MINUTES", "30"))
)
MIGRATION_DRAIN_TIMEOUT_S = float(os.environ.get("MIGRATION_DRAIN_TIMEOUT_S", "600"))
# staleness de routing_state presa em loading/migrating (ver
# reconcile_stale_routes_once) — bem acima de LORA_LOAD_TIMEOUT_S/
# MIGRATION_DRAIN_TIMEOUT_S de propósito, pra nunca competir com uma
# operação genuinamente em andamento e só pegar o que ficou preso de fato
STALE_ROUTE_THRESHOLD_S = float(os.environ.get("STALE_ROUTE_THRESHOLD_S", "1800"))
STALE_ROUTE_CHECK_INTERVAL_S = float(os.environ.get("STALE_ROUTE_CHECK_INTERVAL_S", "300"))
# lifecycle de máquinas: consolidação (esvaziar máquina quase vazia migrando
# as contas pra outra do mesmo template), auto-pausa (stopPod) de máquina sem
# nenhuma atividade e auto-wake (startPod) quando chega request sem nenhuma
# máquina running com vaga. Ambos exigem RUNPOD_API_KEY.
MACHINE_IDLE_STOP_MINUTES = float(os.environ.get("MACHINE_IDLE_STOP_MINUTES", "30"))
WAKE_COOLDOWN_S = float(os.environ.get("WAKE_COOLDOWN_S", "120"))
CONSOLIDATION_INTERVAL_S = float(os.environ.get("CONSOLIDATION_INTERVAL_S", "300"))
CONSOLIDATION_MAX_ORIGIN_ROUTES = int(os.environ.get("CONSOLIDATION_MAX_ORIGIN_ROUTES", "2"))
STOP_RECHECK_GRACE_S = float(os.environ.get("STOP_RECHECK_GRACE_S", "5"))
RUNPOD_API_KEY = os.environ.get("RUNPOD_API_KEY", "")

# provisionamento automático de máquina (3º nível da cascata de alocação,
# além de rodando-com-vaga e despausar): o gateway nunca fala com a API de
# criação da RunPod diretamente — chama de volta o painel Next.js (que já
# tem toda a lógica de GPU/template/stockout), via STAC_LLM_PANEL_URL protegido
# por um secret dedicado (não reaproveita GATEWAY_ADMIN_SECRET — ver README).
# Sem STAC_LLM_PANEL_URL/PANEL_ADMIN_SECRET, esse nível fica desligado (mesmo
# padrão de RUNPOD_API_KEY ausente).
PANEL_URL = os.environ.get("STAC_LLM_PANEL_URL", "").rstrip("/")
PANEL_ADMIN_SECRET = os.environ.get("PANEL_ADMIN_SECRET", "")
PANEL_PROVISION_TIMEOUT_S = float(os.environ.get("PANEL_PROVISION_TIMEOUT_S", "60"))
# intervalo mínimo entre tentativas de criação por plano — evita bombardear
# o painel/RunPod com requests concorrentes ou stockout repetido
PROVISION_COOLDOWN_S = float(os.environ.get("PROVISION_COOLDOWN_S", "180"))
# criar+subir é mais lento que só religar (pode incluir pull de imagem e
# download de pesos do zero num host novo) — Retry-After maior que o do wake
PROVISION_RETRY_AFTER_S = float(os.environ.get("PROVISION_RETRY_AFTER_S", "120"))
# recriação (delete + create + start num host novo): destrutiva e cara, então
# cooldown por máquina mais folgado que o do wake. Retry-After alinhado ao do
# provisionamento (é o mesmo custo de subir um pod do zero).
RECREATE_COOLDOWN_S = float(os.environ.get("RECREATE_COOLDOWN_S", "300"))
RECREATE_RETRY_AFTER_S = float(os.environ.get("RECREATE_RETRY_AFTER_S", "120"))
# soma mínima de slots livres do plano (running + a capacidade cheia de uma
# reserva pausada) — abaixo disso, o loop proativo cria+pausa uma máquina nova
MACHINE_POOL_WATERMARK_SLOTS = float(os.environ.get("MACHINE_POOL_WATERMARK_SLOTS", "5"))
MACHINE_HEALTH_TIMEOUT_S = float(os.environ.get("MACHINE_HEALTH_TIMEOUT_S", "900"))
MACHINE_HEALTH_POLL_INTERVAL_S = float(os.environ.get("MACHINE_HEALTH_POLL_INTERVAL_S", "10"))
# TTL das travas em memória (recreating/provisioning/key_sync): rede de
# segurança contra trava presa. Se a task que deveria liberar a trava morre
# antes do `finally` (GC, exceção fora do try, processo travado), a trava fica
# grudada até restart e todo request seguinte é barrado. Passado o TTL, a trava
# é tratada como vazada e liberada, deixando o fluxo re-disparar. Cada TTL é
# FOLGADAMENTE maior que a duração legítima da sua task — nunca deve expirar no
# meio de uma operação válida (senão dispararia trabalho duplicado, ex.: um 2º
# provisionamento com custo real de GPU):
#   recreate  → task ≤ PANEL_PROVISION_TIMEOUT_S (60s) → 180s
#   provision → task espera health até MACHINE_HEALTH_TIMEOUT_S (900s) → 1200s
#   key_sync  → idem provision (espera health) → 1200s
RECREATE_LOCK_TTL_S = float(os.environ.get("RECREATE_LOCK_TTL_S", "180"))
PROVISION_LOCK_TTL_S = float(os.environ.get("PROVISION_LOCK_TTL_S", "1200"))
KEY_SYNC_LOCK_TTL_S = float(os.environ.get("KEY_SYNC_LOCK_TTL_S", "1200"))
# TTL do cache em memória do interruptor liga/desliga (system_settings) —
# evita 1 round-trip ao Supabase por request na hot path
SETTINGS_CACHE_TTL_S = float(os.environ.get("SETTINGS_CACHE_TTL_S", "30"))
# TTL do cache "chave já upsertada no agent X" — o agent perde as chaves em
# memória a cada restart do pod, então o fluxo base garante a chave via
# upsert lazy antes do proxy; o cache evita 1 round-trip ao agent por request
UPSERT_CACHE_TTL_S = float(os.environ.get("UPSERT_CACHE_TTL_S", "600"))

# ---------- demo pública da landing page (POST /demo) ----------
# O terminal da hero do trystac.com roda inferência de verdade, sem conta e sem
# chave. A política (input, rate limit, system prompt, leitura do stream) mora
# em demo.py — módulo puro; aqui ficam só a configuração e o I/O.
#
# Pod DEDICADO, nunca a alocação de um cliente: o tráfego é anônimo e
# imprevisível, e uma rajada de curiosos na landing page não pode comer as vagas
# de sequência (check_concurrency) de quem paga. A URL vazia desliga a rota
# (404) — é o estado correto de qualquer deploy que não tenha o pod de demo, e
# não há fallback pro pool por construção: o caminho do /demo não passa por
# resolve_route.
DEMO_UPSTREAM_URL = os.environ.get("DEMO_UPSTREAM_URL", "").rstrip("/")
# chave do agent do pod de demo. Server-side, nunca sai daqui: o browser fala
# com /demo sem credencial nenhuma, e é este gateway que autentica no pod.
DEMO_UPSTREAM_KEY = os.environ.get("DEMO_UPSTREAM_KEY", "")
# served_model_name do pod de demo (o alias de --served-model-name). Explícito
# em vez de descoberto via /v1/models pra não pagar um round-trip por request
# e pra o deploy falhar cedo, no boot, se estiver errado.
DEMO_MODEL = os.environ.get("DEMO_MODEL", "")
# origens de browser autorizadas (CSV). Vazio = nenhum browser (fail-closed);
# ver a discussão de o que CORS resolve e o que não em demo.py.
DEMO_ALLOWED_ORIGINS = demo.parse_origins(os.environ.get("DEMO_ALLOWED_ORIGINS"))
# 5 por hora por IP: o suficiente pra experimentar a hero (a pessoa faz 1-3
# perguntas), longe do necessário pra usar a demo como API grátis.
DEMO_LIMIT_PER_IP = int(os.environ.get("DEMO_LIMIT_PER_IP", "5"))
DEMO_LIMIT_WINDOW_S = float(os.environ.get("DEMO_LIMIT_WINDOW_S", "3600"))
# Teto GLOBAL da rota, na mesma janela. O limite por IP é falsificável na
# prática — X-Forwarded-For é um header, e sem Cloudflare na frente qualquer um
# escreve o que quiser nele (ver client_identity.client_ip) — então ele sozinho
# não protege a GPU. Este teto é o que garante que o custo máximo da demo é
# conhecido e pequeno, independente de quantos IPs alguém invente.
DEMO_LIMIT_GLOBAL = int(os.environ.get("DEMO_LIMIT_GLOBAL", "300"))
# 20s de read cobre TTFT + 80 tokens num pod dedicado e quente com folga. Curto
# de propósito: o terminal da hero desiste em 5s e volta pra animação, então
# segurar a conexão além disso só ocupa recurso por uma resposta que ninguém
# mais vai ver.
DEMO_UPSTREAM_TIMEOUT_S = float(os.environ.get("DEMO_UPSTREAM_TIMEOUT_S", "20"))

STARTED_AT = time.time()

supa: SupaClient
store: RoutingStore
# proxy para os agents: connect curto (máquina fora do ar → 503 rápido),
# read longo (streams de inferência podem durar minutos)
proxy_client: httpx.AsyncClient
# client da extração de documento: read MUITO mais longo que o proxy_client.
# Não é streaming (o JSON só serve completo), então o read cobre a geração
# INTEIRA — não o gap entre chunks, que é o que os 60s do proxy_client medem.
document_client: httpx.AsyncClient
# client curto pra API de embeddings da OpenAI (RAG do Go)
openai_client: httpx.AsyncClient
# client pra chamar de volta o painel Next.js (POST /api/machines/provision)
panel_client: httpx.AsyncClient
# client do pod de demo (POST /demo) — pool e timeout próprios, isolados do
# proxy_client de propósito (ver lifespan)
demo_client: httpx.AsyncClient

# cache de chaves: key_hash -> (entry | None, expira_em)
key_cache: dict[str, tuple[dict | None, float]] = {}

# cache do interruptor liga/desliga (system_settings.auto_provision_enabled)
auto_provision_cache: tuple[bool, float] | None = None

# requests em voo por (account_id, machine_id) — base do drain da Fase 5.
# Em memória: válido apenas com réplica única do gateway.
in_flight: dict[tuple[str, str], int] = defaultdict(int)

# rate limit (token bucket) por chave — key_hash, não account_id: uma conta
# pode ter várias chaves, cada uma com seu próprio teto de RPM
rate_buckets: dict[str, tuple[float, float]] = {}  # key_hash -> (tokens, last_refill_ts)

# cache curto da quota diária de tokens: account_id -> (tokens_usados, expira_em)
token_usage_cache: dict[str, tuple[int, float]] = {}

# rate limit da demo pública: por IP e um teto global na mesma janela (ver
# DEMO_LIMIT_GLOBAL). Janela deslizante, não token bucket — o motivo está no
# docstring de demo.SlidingWindowLimiter.
demo_ip_limiter = demo.SlidingWindowLimiter(DEMO_LIMIT_PER_IP, DEMO_LIMIT_WINDOW_S)
demo_global_limiter = demo.SlidingWindowLimiter(DEMO_LIMIT_GLOBAL, DEMO_LIMIT_WINDOW_S)
# desligado quando falta configuração OU quando a URL configurada é a de uma
# máquina do pool (assert_demo_pod_is_dedicated, no boot)
demo_enabled = bool(DEMO_UPSTREAM_URL and DEMO_UPSTREAM_KEY and DEMO_MODEL)

# ambientes já admitidos por stack: stack_id -> {fingerprint: último toque}.
# Só o CAMINHO FRIO (fingerprint novo ou toque vencido) fala com o banco, então
# o teto de ambientes custa zero I/O nos ~99,9% de requests que vêm de um lugar
# já conhecido — mesma disciplina de maybe_touch.
#
# Diferente de rate_buckets/in_flight, este cache NÃO é a fonte da verdade: a
# decisão de admissão é da RPC touch_stack_client (migration 0051), então mais
# de uma réplica do gateway não fura o teto — no pior caso repete um round-trip.
client_seen: dict[str, dict[str, float]] = defaultdict(dict)

# último touch por conta e por máquina (throttle)
last_touch: dict[str, float] = {}
last_machine_touch: dict[str, float] = {}
# último touch de stacks.last_activity_at por stack (throttle) — relógio de
# ociosidade do modelo base, base do reap_idle_base_stacks_once (lifecycle)
last_stack_touch: dict[str, float] = {}

# última tentativa de auto-wake por máquina — evita tempestade de startPod
# com requests concorrentes ou falhas repetidas (ex.: host sem GPU livre)
last_wake_attempt: dict[str, float] = {}

# chaves já garantidas no agent: (key_hash, machine_id) -> expira_em.
# Invalidado por máquina a cada religada (o agent volta sem chaves).
agent_key_upserts: dict[tuple[str, str], float] = {}

# máquinas com re-sync de chaves agendado/em andamento (pós-religada) —
# mesma disciplina do provisioning_in_progress
key_sync_in_progress: dict[str, float] = {}

# serializa a realocação de stacks por plano: escolher alvo + contar vaga +
# repontar precisa ser atômico entre requests concorrentes (réplica única)
realloc_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# provisionamento automático: plano -> criação em andamento (cascata reativa
# e reposição proativa compartilham essa trava, pra nunca criar 2 máquinas
# concorrentes pro mesmo plano) e última tentativa (cooldown)
provisioning_in_progress: dict[str, float] = {}
last_provision_attempt: dict[str, float] = {}

# recriação automática: máquina -> recriação em andamento (trava contra
# requests concorrentes recriarem o mesmo pod) e última tentativa (cooldown).
# Disparada quando o auto-wake falha por "not enough free GPUs" — o host cedeu
# a GPU do pod pausado e só recriar num host novo o traz de volta.
recreating_in_progress: dict[str, float] = {}
last_recreate_attempt: dict[str, float] = {}
# fila de recriações pendentes: máquinas que o caminho reativo (no_gpu) marcou
# pra recriar e ainda não confirmaram sucesso. O lifecycle loop reprocessa
# (process_pending_recreates_once) — mesma disciplina do pending_unloads —
# garantindo o retry se a chamada ao painel falhar/cair, sem depender de um
# request específico. Uma máquina sai da fila quando a recriação conclui (ou
# quando ela deixa de estar stopped/error, ex.: subiu por outro caminho).
pending_recreates: set[str] = set()

# spawn_tracked (referência forte às tasks fire-and-forget), lock_active (travas
# com TTL) e is_no_gpu_error vivem em recovery.py — puros e testáveis sem
# fastapi/env (test_recovery.py). Os dicts-trava e os *_LOCK_TTL_S ficam aqui.

logger = logging.getLogger("gateway")

lifecycle_mgr: "LifecycleManager"
runpod_client: RunPodClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global supa, store, proxy_client, document_client, openai_client, panel_client, demo_client, lifecycle_mgr, runpod_client
    supa = SupaClient(SUPABASE_URL, SERVICE_ROLE_KEY, LORA_BUCKET)
    store = RoutingStore(SUPABASE_URL, SERVICE_ROLE_KEY)
    # read curto (60s): o Cloudflare na frente do RunPod às vezes derruba (RST)
    # conexões TCP em keep-alive; sem isso, uma conexão zumbi reaproveitada
    # do pool prendia o cliente por até 600s esperando um socket morto.
    # 60s é folgado pro maior gap real entre chunks de streaming — TTFT e
    # geração ficam bem abaixo disso mesmo sob 20 concorrentes.
    # retries=2: mesmo com keepalive_expiry curto, uma conexão do pool pode
    # ser resetada pelo Cloudflare ENQUANTO ociosa dentro da janela de expiry
    # — a primeira escrita nela falha na hora (ConnectError). O transporte do
    # httpx detecta e reabre uma conexão nova automaticamente antes de
    # qualquer byte ir pro cliente (visto sob concorrência 5-20: sem isso,
    # a requisição inteira falhava em ~3s com corpo vazio, sem erro visível).
    proxy_client = httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=5.0, write=10.0, pool=10.0),
        limits=httpx.Limits(
            max_connections=100, max_keepalive_connections=20, keepalive_expiry=5.0
        ),
        transport=httpx.AsyncHTTPTransport(retries=2),
    )
    # não-streaming: o read tem que cobrir a geração inteira do JSON, então o
    # teto é a DURAÇÃO da inferência, não o silêncio entre chunks (os 60s do
    # proxy_client). Um documento longo com schema grande pode passar de
    # 2 minutos; abaixo disso a chamada morria com o pod ainda trabalhando.
    document_client = httpx.AsyncClient(
        timeout=httpx.Timeout(DOCUMENT_UPSTREAM_TIMEOUT_S, connect=5.0, write=10.0, pool=10.0),
        # limits explícito (mesmos valores do proxy_client) e não por default do
        # httpx: o keepalive_expiry curto é o fix do RST do Cloudflare na frente
        # do RunPod, e depender do default deixaria essa proteção à mercê de uma
        # mudança de versão da lib.
        limits=httpx.Limits(
            max_connections=100, max_keepalive_connections=20, keepalive_expiry=5.0
        ),
        transport=httpx.AsyncHTTPTransport(retries=2),
    )
    openai_client = httpx.AsyncClient(
        base_url="https://api.openai.com/v1",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        timeout=httpx.Timeout(20.0, connect=5.0),
    )
    panel_client = httpx.AsyncClient(
        timeout=httpx.Timeout(PANEL_PROVISION_TIMEOUT_S, connect=5.0)
    )
    # client próprio da demo pública, e não o proxy_client, por duas razões:
    # o read é curto (DEMO_UPSTREAM_TIMEOUT_S, ver o comentário lá) e o pool de
    # conexões é separado — uma rajada de tráfego anônimo na landing page não
    # pode disputar conexão com as requests de cliente pagante.
    demo_client = httpx.AsyncClient(
        headers={
            "Authorization": f"Bearer {DEMO_UPSTREAM_KEY}",
            "Content-Type": "application/json",
        },
        timeout=httpx.Timeout(DEMO_UPSTREAM_TIMEOUT_S, connect=5.0, write=10.0, pool=5.0),
        limits=httpx.Limits(
            max_connections=20, max_keepalive_connections=5, keepalive_expiry=5.0
        ),
        transport=httpx.AsyncHTTPTransport(retries=2),
    )
    await assert_demo_pod_is_dedicated()
    if RUNPOD_API_KEY:
        runpod_client = RunPodClient(RUNPOD_API_KEY)
    else:
        runpod_client = None
        logger.warning(
            "RUNPOD_API_KEY ausente — auto-pausa e auto-wake de máquinas desligados"
        )
    if not PANEL_URL or not PANEL_ADMIN_SECRET:
        logger.warning(
            "STAC_LLM_PANEL_URL/PANEL_ADMIN_SECRET ausente — provisionamento automático de máquina desligado"
        )
    lifecycle_mgr = LifecycleManager(
        store=store,
        supa=supa,
        call_agent=call_agent,
        in_flight=in_flight,
        idle_unload_minutes=IDLE_RELEASE_MINUTES,
        drain_timeout_s=MIGRATION_DRAIN_TIMEOUT_S,
        lora_load_timeout_s=LORA_LOAD_TIMEOUT_S,
        machine_free_slots=machine_free_slots,
        runpod=runpod_client,
        machine_idle_stop_minutes=MACHINE_IDLE_STOP_MINUTES,
        consolidation_max_origin_routes=CONSOLIDATION_MAX_ORIGIN_ROUTES,
        stop_recheck_grace_s=STOP_RECHECK_GRACE_S,
        try_provision_for_pool=try_provision_for_pool,
        pool_watermark_slots=MACHINE_POOL_WATERMARK_SLOTS,
        auto_provision_enabled=auto_provision_enabled,
        on_machine_running=handle_machine_running,
        vllm_health_check=check_vllm_health,
        try_recreate_machine=try_recreate_machine,
        pending_recreates=pending_recreates,
    )
    reaper_task = asyncio.create_task(lifecycle_mgr.idle_reaper_loop())
    machine_task = asyncio.create_task(
        lifecycle_mgr.machine_lifecycle_loop(CONSOLIDATION_INTERVAL_S)
    )
    metrics_task = asyncio.create_task(metrics_collection_loop())
    stale_routes_task = asyncio.create_task(stale_route_reconciliation_loop())
    usage_class_task = asyncio.create_task(usage_class_loop())
    billing_task = asyncio.create_task(billing_reconcile_loop())
    yield
    reaper_task.cancel()
    machine_task.cancel()
    metrics_task.cancel()
    stale_routes_task.cancel()
    usage_class_task.cancel()
    billing_task.cancel()
    await proxy_client.aclose()
    await document_client.aclose()
    await openai_client.aclose()
    await panel_client.aclose()
    await demo_client.aclose()
    await store.aclose()
    await supa.aclose()
    if runpod_client:
        await runpod_client.aclose()


app = FastAPI(lifespan=lifespan)


@app.exception_handler(ContextWindowExceeded)
async def context_window_exceeded_handler(request: Request, exc: ContextWindowExceeded):
    """Formata o estouro de contexto no shape do cliente. Handler dedicado à
    SUBCLASSE (o Starlette resolve por MRO, então vence o default de
    HTTPException) — os demais HTTPException seguem no {"detail": ...} padrão,
    que painel/scripts já parseiam.

    O shape vem da EXCEÇÃO, não da URL: quem sabe qual protocolo está sendo
    atendido é o handler que levantou o erro. O fallback por path continua pros
    call sites que não passam shape (documents/*, images/*, catch-all)."""
    shape = getattr(exc, "shape", None)
    if not shape:
        shape = "anthropic" if request.url.path.startswith("/v1/messages") else "openai"
    return JSONResponse(status_code=exc.status_code, content=error_body_for(shape, exc.detail))


# O gateway é uma API pra clientes programáticos (SDK OpenAI/Anthropic,
# Codex/Claude Code, BYOE) — nunca chamada de um browser. CORS explícito e
# fechado em vez de ausente (o padrão do Starlette sem CORSMiddleware
# nenhum já bloqueia por padrão, mas fica implícito; aqui fica documentado
# e fácil de abrir uma origem específica no futuro, se algum dia existir
# um playground no browser).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "x-api-key", "Content-Type", "anthropic-version"],
)


@app.middleware("http")
async def reject_oversized_upload(request: Request, call_next):
    """Recusa upload absurdo pelo Content-Length, ANTES de ler o corpo.

    Existe porque o /v1/documents/extract declara `file: UploadFile`, e aí o
    Starlette parseia o multipart (spoolando em disco o que passa de 1 MB)
    ANTES de a função do endpoint rodar — ou seja, antes do authenticate e
    antes de qualquer teto por plano. Sem este guard, um `curl -F
    file=@10GB.bin` SEM chave nenhuma enchia o disco do container: DoS anônimo
    e repetível.

    Middleware e não dependência do endpoint justamente por causa dessa ordem:
    aqui roda antes do parsing; lá dentro, depois.

    Sem Content-Length (chunked) não há o que checar — segue e cai no teto de
    `file.size` depois do parse. Vale a mesma lógica do MAX_BODY_BYTES do
    proxy: isto é defesa contra corpo absurdo, não o controle de custo fino
    (que é rate limit + quota + tetos por plano).

    /v1/images/extract entra no mesmo guard que /v1/documents/extract, com o
    teto de bytes de imagem (menor, ver document_extract.py) em vez do de
    documento.

    /v1/documents/generate entra no mesmo guard: o corpo ali é JSON (sem
    multipart, sem spool em disco), mas o FastAPI ainda lê o corpo inteiro
    pra RAM antes do handler rodar — sem este corte, um Content-Length
    gigante é lido por completo antes de qualquer autenticação."""
    if request.url.path == "/v1/documents/extract":
        declared = request.headers.get("content-length")
        if declared and declared.isdigit():
            # margem sobre o teto de bytes do documento: o corpo multipart
            # carrega também o schema (até MAX_SCHEMA_BYTES) e o overhead das
            # boundaries, então comparar cru contra o teto do arquivo recusaria
            # upload legítimo no limite.
            ceiling = document_extract.max_limit_bytes() + MAX_SCHEMA_BYTES + 65536
            if int(declared) > ceiling:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "corpo da requisição excede o limite"},
                )
    elif request.url.path == "/v1/images/extract":
        declared = request.headers.get("content-length")
        if declared and declared.isdigit():
            ceiling = document_extract.max_limit_image_bytes() + MAX_SCHEMA_BYTES + 65536
            if int(declared) > ceiling:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "corpo da requisição excede o limite"},
                )
    elif request.url.path == "/v1/documents/generate":
        declared = request.headers.get("content-length")
        if declared and declared.isdigit():
            # margem sobre o teto de bytes do HTML: o corpo JSON carrega o
            # HTML dentro de uma chave (`{"html": "..."}`), então o overhead
            # da própria envelope JSON também precisa de folga.
            ceiling = document_generate.max_limit_bytes() + 4096
            if int(declared) > ceiling:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "corpo da requisição excede o limite"},
                )
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    # não mexe em headers de cache/transformação: quebraria as respostas
    # SSE (text/event-stream) do proxy de streaming
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    # MutableHeaders do Starlette não tem .pop() (só __delitem__) — usar
    # .pop() aqui derrubava TODA requisição do gateway com 500
    if "Server" in response.headers:
        del response.headers["Server"]
    return response


def require_admin(secret: str | None):
    if not GATEWAY_ADMIN_SECRET or not secret or not hmac.compare_digest(secret, GATEWAY_ADMIN_SECRET):
        raise HTTPException(status_code=401, detail="admin secret inválido")


def lora_name(stack_id: str) -> str:
    # Nome do adapter LoRA no vLLM, escopado por STACK (migration 0029). O
    # prefixo "acct-" é mantido por compatibilidade: os filtros de /v1/models
    # (gateway e agent) removem qualquer id que comece com "acct-", então trocar
    # o prefixo exigiria atualizar os dois. Duas stacks da mesma conta agora
    # recebem nomes distintos (acct-<stackA> ≠ acct-<stackB>) — fim da colisão.
    return f"acct-{stack_id}"


# ---------- Autenticação ----------


def billing_blocked(stack: dict) -> str | None:
    """Motivo do corte por inadimplência, ou None se a stack pode trafegar.

    Única implementação da regra: `authenticate` a aplica por request (corte
    exato) e `billing_reconcile_once` (lifecycle.py) a usa pra materializar o
    corte em stacks.billing_status, pra tabela e comportamento não divergirem.

    'past_due' NÃO bloqueia por si só — é o par (past_due, past_due_since)
    contra BILLING_GRACE_HOURS que decide. Um cliente cuja fatura falhou hoje
    continua trabalhando; quem passou das 72h para.
    """
    status = (stack.get("billing_status") or "active").lower()
    if status in ("suspended", "canceled"):
        return "assinatura suspensa por falta de pagamento — regularize em app.trystac.com"

    past_due_since = stack.get("past_due_since")
    if not past_due_since:
        return None
    try:
        since = datetime.fromisoformat(str(past_due_since).replace("Z", "+00:00"))
        if since.tzinfo is None:
            # A coluna é timestamptz e o PostgREST sempre emite offset, mas um
            # valor naive aqui compararia com datetime.now(timezone.utc) e
            # levantaria TypeError DENTRO de authenticate — ou seja, 500 em
            # todo o tráfego da chave, o oposto do fail-open pretendido.
            raise ValueError("timestamp sem timezone")
    except ValueError:
        # Timestamp ilegível não é motivo pra derrubar um cliente pagante:
        # fail-open aqui é a escolha certa (o oposto do fail-closed de
        # stack_id, onde a ausência do dado significa configuração quebrada).
        # O corte não se perde: billing_reconcile_once filtra past_due_since no
        # próprio banco, sem passar por este parser.
        logger.warning("past_due_since ilegível na stack %s: %r", stack.get("id"), past_due_since)
        return None

    if since + timedelta(hours=BILLING_GRACE_HOURS) <= datetime.now(timezone.utc):
        return (
            f"assinatura em atraso há mais de {int(BILLING_GRACE_HOURS)}h — "
            "regularize o pagamento em app.trystac.com"
        )
    return None


# Teto de entradas do client_seen por stack. Ambiente ADMITIDO já é limitado
# pelo cap do plano, mas ambiente NEGADO não é — um cliente variando o
# User-Agent geraria uma entrada nova por request. A poda mantém o dicionário
# do tamanho de "o que ainda está dentro do throttle".
CLIENT_SEEN_MAX_PER_STACK = 256


def _remember_client(seen: dict, fingerprint: str, now: float, admitted: bool) -> None:
    if len(seen) >= CLIENT_SEEN_MAX_PER_STACK:
        cutoff = now - CLIENT_TOUCH_THROTTLE_S
        for stale in [fp for fp, (ts, _) in seen.items() if ts < cutoff]:
            del seen[stale]
    seen[fingerprint] = (now, admitted)


def _client_limit_detail(plan: str | None, cap: int | None) -> str:
    return (
        f"limite de {cap} ambiente(s) simultâneos do plano {plan} atingido — "
        "libere um ambiente em app.trystac.com ou faça upgrade do plano"
    )


async def enforce_client_limit(entry: dict, stack: dict, plan: str | None, headers) -> None:
    """Teto de LUGARES distintos conectados à stack (migration 0051).

    Complementa o teto de CHAVES por stack aplicado na emissão pelo painel
    (MAX_KEYS_BY_PLAN em lib/types.ts): aquele é o contrato, este pega quem
    usa uma única chave em toda a equipe. Ver o docstring de
    client_identity.py para o que o fingerprint acerta e o que ele erra.

    Custo no caminho quente: zero. Ambiente conhecido dentro do throttle sai
    pelo cache em memória sem nenhum I/O — só fingerprint novo (ou toque
    vencido) chama a RPC, que é quem de fato decide a admissão.

    Falha de I/O é fail-open: registrar de onde o cliente conecta é
    telemetria com dente, e telemetria nunca pode derrubar inferência."""
    stack_id = stack.get("id")
    if not stack_id:
        return

    fingerprint, label, user_agent, bucket = client_fingerprint(headers)
    cap = client_cap(plan)
    now = time.time()
    seen = client_seen[stack_id]

    cached = seen.get(fingerprint)
    if cached and now - cached[0] < CLIENT_TOUCH_THROTTLE_S:
        if cached[1]:
            return
        # negado e ainda dentro do throttle: repete o 403 sem bater no banco,
        # senão um cliente bloqueado gera uma RPC por request. Liberar a vaga
        # no painel chama /admin/flush-key-cache, que zera este cache também.
        raise HTTPException(status_code=403, detail=_client_limit_detail(plan, cap))

    try:
        admitted, used = await supa.touch_stack_client(
            stack_id=stack_id,
            account_id=entry.get("account_id"),
            api_key_id=entry.get("id"),
            fingerprint=fingerprint,
            label=label,
            user_agent=user_agent[:MAX_USER_AGENT_CHARS] if user_agent else None,
            ip_bucket=bucket,
            window_days=CLIENT_WINDOW_DAYS,
            # Em observação o teto NÃO vai para o banco de propósito: a RPC
            # não grava ambiente negado, então mandar o cap aqui esconderia
            # justamente o excesso que a fase de observação existe para medir.
            cap=cap if CLIENT_LIMIT_ENFORCE else None,
        )
    except Exception as e:
        # Marca como admitido para dar BACKOFF: sem isto, uma RPC indisponível
        # (migration 0051 ainda não aplicada, Supabase fora do ar) custaria um
        # round-trip perdido em TODO request, não só no primeiro de cada
        # ambiente. Fail-open — telemetria com dente jamais derruba inferência.
        _remember_client(seen, fingerprint, now, True)
        logger.warning("registro de ambiente falhou (stack=%s): %s", stack_id, e)
        return

    _remember_client(seen, fingerprint, now, admitted)

    if admitted:
        if cap is not None and used > cap:
            logger.warning(
                "ambientes acima do teto (observação): stack=%s plano=%s %d/%d "
                "cliente=%s rede=%s",
                stack_id, plan, used, cap, label, bucket,
            )
        return

    # gateway_requests não registra rejeição pré-rota (migration 0038), então
    # esta linha é a única trilha que o suporte tem do corte.
    logger.warning(
        "ambiente bloqueado: stack=%s plano=%s %d/%s cliente=%s rede=%s",
        stack_id, plan, used, cap, label, bucket,
    )
    raise HTTPException(status_code=403, detail=_client_limit_detail(plan, cap))


def enforce_cli_policy(stack: dict, plan: str | None, path: str | None, headers) -> None:
    """Corte de CLI por plano (o Go não coda — ver cli_policy.py).

    Custo no caminho quente: zero I/O. `plan` e `stack` já vieram de
    resolve_key_stack (cache de chave em memória), e a decisão é uma função pura
    dos três sinais.

    Em observação (CLI_POLICY_ENFORCE=0) loga WARNING e deixa passar — é o que
    permite ver quem seria cortado sem cortar, caso um cliente legítimo apareça
    barrado pela camada de User-Agent."""
    reason = cli_block_reason(plan, path, headers.get("user-agent"))
    if not reason:
        return

    if not CLI_POLICY_ENFORCE:
        logger.warning(
            "CLI no plano %s (observação, não cortado): stack=%s path=%s ua=%r",
            plan, stack.get("id"), path, headers.get("user-agent"),
        )
        return

    # gateway_requests não registra rejeição pré-rota (migration 0038), então
    # esta linha é a única trilha que o suporte tem do corte — mesmo motivo do
    # log em enforce_client_limit.
    logger.warning(
        "CLI bloqueado: stack=%s plano=%s path=%s ua=%r",
        stack.get("id"), plan, path, headers.get("user-agent"),
    )
    # 403 e não 402/401: a credencial está intacta e o cliente não deve nada —
    # o plano simplesmente não inclui o recurso. Um 401 mandaria o cliente
    # trocar a chave (não resolve) e um 402 diria que há pagamento pendente
    # (não há); 403 é o único que descreve "sua chave, sem esse direito".
    raise HTTPException(status_code=403, detail=reason)


async def authenticate(
    authorization: str | None, headers, path: str | None = None
) -> tuple[dict, str]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="chave de acesso ausente")
    key = authorization.removeprefix("Bearer ").strip()
    key_hash = hashlib.sha256(key.encode()).hexdigest()

    cached = key_cache.get(key_hash)
    if cached and cached[1] > time.time():
        entry = cached[0]
    else:
        entry = await supa.find_active_key(key_hash)
        ttl = KEY_CACHE_TTL_S if entry else KEY_CACHE_NEGATIVE_TTL_S
        key_cache[key_hash] = (entry, time.time() + ttl)

    if not entry:
        raise HTTPException(status_code=401, detail="chave de acesso inválida")

    # checado por request, não só num filtro na query: o key_cache (TTL de
    # KEY_CACHE_TTL_S) manteria uma chave expirada válida por até mais um
    # TTL depois do vencimento se a expiração dependesse só do PostgREST
    expires_at = entry.get("expires_at")
    if expires_at:
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            expiry = None
        if expiry and expiry <= datetime.now(timezone.utc):
            raise HTTPException(status_code=401, detail="chave expirada")

    # fail-closed: toda chave precisa de stack_id resolvido. Plano e
    # system_prompt são propriedade da stack (migration 0027 removeu
    # accounts.plan/system_prompt) — sem stack não há config nenhuma pra
    # resolver, então não existe mais um "roteamento por plano puro" de
    # fallback. A migration 0021 já fez o backfill de stack_id em toda
    # chave ativa de conta com stack; esta checagem é o defense-in-depth
    # pra qualquer chave que escape disso no futuro.
    if not entry.get("stack_id"):
        raise HTTPException(status_code=401, detail="chave sem stack associada — contate o suporte")

    # Corte por inadimplência. Fica aqui, e não junto de check_rate_limit nos
    # 4 call-sites de inferência, por dois motivos: authenticate_anthropic
    # delega pra cá (então /v1/messages fica coberto de graça), e endpoint
    # novo nasce protegido em vez de nascer aberto.
    #
    # 402 e não 401/403: o cliente não errou a credencial nem perdeu
    # permissão — a chave está intacta e volta a funcionar sozinha assim que
    # o pagamento entrar. Revogar a chave resolveria o corte, mas obrigaria o
    # cliente a trocá-la em todas as integrações dele pra voltar.
    #
    # Playground é isento (mesmo critério de check_token_quota): é a chave
    # interna que o admin usa pra diagnosticar a stack. Bloqueá-la tiraria
    # justamente a ferramenta de investigar a conta suspensa.
    if entry.get("purpose") != "playground":
        stack, plan = resolve_key_stack(entry)
        if stack:
            reason = billing_blocked(stack)
            if reason:
                raise HTTPException(status_code=402, detail=reason)
            # Corte de CLI por plano, aqui pelos MESMOS dois motivos do bloco de
            # billing: /v1/messages fica coberto de graça (authenticate_anthropic
            # delega pra cá) e endpoint novo nasce protegido. Antes do teto de
            # ambientes de propósito — não faz sentido gastar uma vaga de
            # ambiente com uma requisição que vai levar 403 na linha seguinte.
            #
            # Playground é isento pelo mesmo critério de billing_blocked: é a
            # chave interna com que o admin diagnostica a stack, e diagnosticar
            # inclui reproduzir o que o cliente faz.
            enforce_cli_policy(stack, plan, path, headers)
            # Mesmo motivo do bloco acima para morar aqui, e mesma isenção de
            # playground: a chave interna do admin não é um "lugar" do cliente
            # e não pode consumir a vaga que ela existe para diagnosticar.
            await enforce_client_limit(entry, stack, plan, headers)

    return entry, key_hash


def check_rate_limit(key_hash: str, plan: str | None) -> None:
    """Token bucket em memória por chave: rpm do plano tokens/min, com burst
    até o teto do bucket. Estourou -> 429 + Retry-After; nunca enfileira, só
    rejeita — o cliente decide se tenta de novo."""
    rpm = rate_limit_rpm(plan)
    now = time.time()
    tokens, last = rate_buckets.get(key_hash, (rpm, now))
    tokens = min(rpm, tokens + (now - last) * rpm / 60.0)
    if tokens < 1.0:
        rate_buckets[key_hash] = (tokens, now)
        retry_after = max(1, int((1.0 - tokens) * 60.0 / rpm) + 1)
        raise HTTPException(
            status_code=429,
            detail="limite de requisições excedido, tente novamente em instantes",
            headers={"Retry-After": str(retry_after)},
        )
    rate_buckets[key_hash] = (tokens - 1.0, now)


async def check_token_quota(account_id: str, plan: str | None, purpose: str = "customer") -> None:
    """Quota diária de tokens por conta — protege contra custo real (poucas
    requests, cada uma gerando muito token), o que rate limit/concorrência
    por si não cobrem. Lida de usage_metrics via account_token_usage_today,
    com cache curto (TOKEN_QUOTA_CACHE_TTL_S) pra não bater no Supabase a
    cada request. 0/plano sem entrada = sem teto (opt-in por plano); `plan`
    None (chave sem stack resolvível) cai no mesmo caso — resolve_route
    rejeita a request logo em seguida com 503, então não enforça nada aqui
    à toa.

    `purpose == "playground"` (migration 0044, chave interna gerada junto com
    a stack pro admin testar) nunca tem cota — não é uso do cliente, e
    account_token_usage_today já exclui essas chaves da soma pra não inflar a
    cota "customer" da mesma conta."""
    if purpose == "playground":
        return
    budget = DAILY_TOKEN_BUDGET.get(plan, 0)
    if budget <= 0:
        return
    now = time.time()
    cached = token_usage_cache.get(account_id)
    if cached and cached[1] > now:
        used = cached[0]
    else:
        used = await supa.account_token_usage_today(account_id)
        token_usage_cache[account_id] = (used, now + TOKEN_QUOTA_CACHE_TTL_S)
    if used >= budget:
        raise HTTPException(
            status_code=429,
            detail=f"quota diária de tokens excedida ({used}/{budget})",
            headers={"Retry-After": "3600"},
        )


async def authenticate_anthropic(
    authorization: str | None, x_api_key: str | None, headers, path: str | None = None
) -> tuple[dict, str, str]:
    """Igual a authenticate, mas aceita a chave em Authorization: Bearer OU
    x-api-key — o Claude Code manda num dos dois (às vezes os dois, se o
    usuário configurou apiKeyHelper) dependendo de ANTHROPIC_AUTH_TOKEN vs
    ANTHROPIC_API_KEY. Devolve também o header já normalizado pra "Bearer
    <key>", pra repassar ao agent no upstream (que só entende Bearer, nunca
    x-api-key)."""
    bearer = None
    if authorization and authorization.startswith("Bearer "):
        bearer = authorization.removeprefix("Bearer ").strip()
    key = bearer or (x_api_key.strip() if x_api_key else None)
    if not key:
        raise HTTPException(status_code=401, detail="chave de acesso ausente")
    bearer_header = f"Bearer {key}"
    entry, key_hash = await authenticate(bearer_header, headers, path)
    return entry, key_hash, bearer_header


# ---------- Roteamento / alocação ----------


async def call_agent(machine: dict, path: str, body: dict, timeout_s: float = 30.0) -> dict:
    """POST /admin/* no agent do pod, autenticado pelo admin_secret da máquina."""
    try:
        r = await proxy_client.post(
            f"{machine['public_url']}/admin{path}",
            json=body,
            headers={"X-Admin-Secret": machine["admin_secret"]},
            timeout=httpx.Timeout(timeout_s, connect=5.0),
        )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"máquina indisponível: {e}")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"agent {path} → {r.status_code}: {r.text}")
    return r.json()


async def call_vllm_tokenize(machine: dict, model: str, text: str) -> int | None:
    """Contagem real de tokens via /admin/tokenize do agent (que chama o
    /tokenize do vLLM local). None em qualquer falha — o chamador cai de
    volta pra estimativa heurística; esta checagem extra nunca pode travar
    uma request por conta própria (pod fora do ar, agent antigo sem o
    endpoint, timeout — tudo cai no mesmo None)."""
    try:
        resp = await call_agent(machine, "/tokenize", {"text": text, "model": model}, timeout_s=8.0)
    except HTTPException:
        return None
    count = resp.get("count")
    return count if isinstance(count, int) else None


async def resolve_est_tokens(
    machine: dict, heuristic_est: int, exact_text: str, image_tokens: int = 0
) -> tuple[int, EstimateKind]:
    """Wrapper de context_budget.resolve_est_tokens fechando o tokenize_fn.

    A lógica mora no módulo puro (testável sem as env vars obrigatórias daqui);
    aqui só se amarra a implementação concreta da contagem."""
    return await _resolve_est_tokens(
        machine, heuristic_est, exact_text, call_vllm_tokenize, image_tokens=image_tokens
    )


async def get_agent_metrics(machine: dict, reset: bool = True) -> dict | None:
    """GET /admin/metrics no agent (call_agent só faz POST) — usado só pela
    coleta periódica de uso abaixo, nunca no caminho de request de cliente.
    None em qualquer falha: agent fora do ar não deve derrubar o loop, os
    contadores seguem acumulando no pod até a próxima coleta."""
    try:
        r = await proxy_client.get(
            f"{machine['public_url']}/admin/metrics",
            params={"reset": "true"} if reset else {},
            headers={"X-Admin-Secret": machine["admin_secret"]},
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


async def check_vllm_health(machine: dict) -> dict | None:
    """GET /health do agent do pod (rota aberta, sem admin secret): devolve
    {vllm_ready, vllm_alive, ...}. Usado pela reconciliação de status para só
    promover a máquina a 'running' quando o vLLM está REALMENTE pronto — o pod
    do RunPod fica RUNNING (contêiner de pé) minutos/dezenas de minutos antes de
    o modelo terminar de carregar; promover cedo faz o relógio de ociosidade
    começar durante o boot e a auto-pausa matar a máquina antes de servir.
    None em qualquer falha (o chamador trata como 'não dá pra confirmar')."""
    url = machine.get("public_url")
    if not url:
        return None
    try:
        r = await proxy_client.get(
            f"{url}/health", timeout=httpx.Timeout(8.0, connect=5.0)
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


async def collect_usage_metrics_once() -> None:
    """Único escritor de usage_metrics: puxa os contadores acumulados do
    agent de cada máquina running (zerando-os na leitura) e grava o delta
    em usage_metrics. Roda sozinha no processo do gateway — antes de existir,
    usage_metrics só era populado quando um admin abria o painel, o que
    deixava a quota diária de tokens (check_token_quota) cega a qualquer
    uso entre duas visitas (esse coletor do painel foi removido depois que
    causou atribuição incorreta a api_key_id=NULL por divergir do esquema
    de chave do agent).

    `per_key` do agent é indexado por api_key_id (não mais por key_prefix):
    o prefixo tem só 32 bits, colisão entre duas contas diferentes não é
    impossível, e usar o prefixo como chave de agregação atribuiria o uso
    de uma conta à outra na eventualidade de colisão — sensível o bastante
    (alimenta a quota diária de custo) pra merecer o identificador estável."""
    machines = await supa.list_running_machines()
    for machine in machines:
        if not machine.get("public_url"):
            continue
        snap = await get_agent_metrics(machine)
        if not snap:
            continue
        active = {
            api_key_id: v for api_key_id, v in (snap.get("per_key") or {}).items()
            if v.get("requests", 0) > 0
        }
        if not active:
            continue
        window_start = datetime.now(timezone.utc).isoformat()
        try:
            stack_by_key = await supa.stack_ids_for_keys(list(active.keys()))
        except Exception as e:
            logger.warning(
                "coleta de métricas: falha ao resolver stack_id das chaves da máquina %s (%s)",
                machine["id"], e,
            )
            stack_by_key = {}
        rows = [
            {
                "api_key_id": api_key_id,
                "machine_id": machine["id"],
                "stack_id": stack_by_key.get(api_key_id),
                "window_start": window_start,
                "requests": v.get("requests", 0),
                "tokens_in": v.get("tokens_in", 0),
                "tokens_out": v.get("tokens_out", 0),
                "concurrent_peak": snap.get("concurrent_peak", 0),
            }
            for api_key_id, v in active.items()
        ]
        try:
            await supa.insert_usage_metrics(rows)
        except Exception as e:
            logger.warning("coleta de métricas: falha ao gravar máquina %s (%s)", machine["id"], e)
        try:
            await supa.touch_keys_last_used(list(active.keys()), window_start)
        except Exception as e:
            logger.warning(
                "coleta de métricas: falha ao tocar last_used_at das chaves da máquina %s (%s)",
                machine["id"], e,
            )


async def metrics_collection_loop(interval_s: float = METRICS_COLLECTION_INTERVAL_S):
    while True:
        await asyncio.sleep(interval_s)
        try:
            await collect_usage_metrics_once()
        except Exception as e:
            logger.warning("coleta periódica de métricas falhou: %s", e)


async def classify_stacks_once() -> int:
    """Reclassifica usage_class das stacks pelo consumo real (usage_metrics
    agregado pela RPC stack_usage_stats, migration 0032). A decisão em si é
    pura (usage_class.classify_stack: dois fatores, histerese, cooldown);
    aqui é só o I/O. Devolve quantas stacks mudaram de classe — o chamador usa
    pra decidir se vale rodar o rebalanceamento de teto de heavy."""
    rows = await supa.stack_usage_stats(USAGE_CLASS_WINDOW_DAYS)
    now = datetime.now(timezone.utc)
    changed = 0
    for row in rows:
        new_class = classify_stack(
            row,
            now,
            min_active_days=USAGE_CLASS_MIN_ACTIVE_DAYS,
            cooldown_days=USAGE_CLASS_COOLDOWN_DAYS,
        )
        if not new_class:
            continue
        try:
            await supa.update_stack_usage_class(row["stack_id"], new_class)
            changed += 1
            logger.info(
                "usage_class: stack %s reclassificada %s -> %s",
                row["stack_id"], row.get("usage_class") or "low", new_class,
            )
        except Exception as e:
            logger.warning(
                "usage_class: falha ao atualizar stack %s (%s)", row["stack_id"], e
            )
    return changed


def _evict_key_cache_for_stack(stack_id: str) -> None:
    """Derruba as entradas do key_cache da stack depois de ela mudar de
    máquina fora do caminho de request.

    Sem isto, requests continuariam sendo roteados pra máquina ANTIGA até o
    TTL do cache expirar. O reallocate_stack não precisa disto porque muta o
    dict do stack in place (ele tem o objeto cacheado em mãos); aqui o
    movimento nasce no loop de background, sem acesso ao objeto cacheado.

    Casa por dois caminhos: `stack_id` direto da chave (migration 0019) e a
    lista `stacks` embutida da conta — chaves legadas sem stack_id gravado só
    são alcançáveis pelo segundo. Mesmo se ambos falhassem, o estrago é
    limitado pelo TTL do key_cache (KEY_CACHE_TTL_S)."""
    stale = [
        kh
        for kh, (entry, _) in key_cache.items()
        if entry
        and (
            entry.get("stack_id") == stack_id
            or any(s.get("id") == stack_id for s in entry.get("stacks") or [])
        )
    ]
    for key_hash in stale:
        key_cache.pop(key_hash, None)


async def relocate_stack_for_balance(stack: dict, reason: str) -> dict | None:
    """Move uma stack pra outra máquina do plano por BALANCEAMENTO (teto de
    heavy estourado, migration 0037) — não por indisponibilidade da origem,
    que é o caso do reallocate_stack.

    Como o reallocate_stack: MOVE as chaves (api_keys.machine_id) em vez de
    criar/revogar, então a plain key configurada no cliente do usuário
    continua funcionando. Diferente dele: roda fora do caminho de request,
    então não há `entry` em mãos — a invalidação de cache e o reenvio de
    chaves são feitos explicitamente no fim.

    None = sem destino com vaga (o chamador dispara a cascata de wake/
    provisionamento) ou perdeu a corrida pra um request concorrente."""
    plan = stack["plan"]
    old_id = stack.get("machine_id")
    if not old_id:
        return None
    async with realloc_locks[plan]:
        fresh = await supa.get_stack(stack["id"])
        if not fresh or fresh.get("machine_id") != old_id:
            return None  # request concorrente já moveu; a próxima passada reavalia
        usage_class = fresh.get("usage_class") or "low"
        target = await pick_running_machine_with_stack_slot(
            plan, exclude_machine_id=old_id, usage_class=usage_class
        )
        if not target:
            return None
        if not await supa.repoint_stack(stack["id"], old_id, target["id"]):
            return None
        await supa.move_account_keys(
            fresh["account_id"], old_id, target["id"], stack_id=stack["id"]
        )

    _evict_key_cache_for_stack(stack["id"])
    # sync nas DUAS pontas: no destino pra chave passar a ser aceita; na
    # ORIGEM porque o /admin/sync-keys do agent faz clear() + substituição
    # total, e sem ele a chave movida continuaria válida em memória no pod
    # antigo — que é alcançável direto pela URL pública da RunPod
    schedule_key_sync(target["id"])
    schedule_key_sync(old_id)
    try:
        await store.record_reallocation(
            fresh["account_id"], from_machine_id=old_id, machine_id=target["id"]
        )
        await supa.log_machine_event(
            target["id"], "stack_migrated",
            f"Stack {fresh.get('slug') or stack['id']} realocada por balanceamento "
            f"de carga ({reason})",
        )
    except Exception:
        pass  # histórico é best-effort, nunca desfaz um movimento já concluído
    logger.info(
        "rebalance: stack %s (%s) movida de %s para %s — %s",
        fresh.get("slug") or stack["id"], fresh.get("usage_class"),
        old_id, target["id"], reason,
    )
    return target


async def rebalance_high_caps_once(retry_budget: int = HIGH_CAP_MAX_RETRIES) -> None:
    """Faz valer o teto de stacks 'high' por máquina (migration 0037).

    Roda depois de cada passada de classificação: uma stack que sobe pra
    'high' pode estourar o teto da máquina onde já está, e é justamente ela
    que sai — list_stacks_on_machine ordena por usage_class_updated_at desc,
    então o excedente despejado é sempre quem mudou de classe mais
    recentemente, nunca o co-tenant que estava lá e não mudou nada.

    Rebaixamento (high -> medium/low) não move ninguém: só LIBERA espaço no
    teto, e o próximo desbalanceamento aproveita.

    A cabeça liberada na origem fica livre pra qualquer classe — o teto de
    heavy é sub-teto, não reserva.

    Só planos de pod compartilhado: Max/Enterprise têm pod dedicado, onde
    "mistura de perfis" não existe."""
    retry_needed = False
    for plan in SHARED_POD_PLANS:
        for machine in await supa.list_running_machines_for_plan(plan):
            cap = await supa.machine_high_cap(machine["id"])
            if cap is None:
                continue  # template sem teto configurado: fail-open
            highs = await supa.list_stacks_on_machine(machine["id"], usage_class="high")
            excess = len(highs) - cap
            if excess <= 0:
                continue
            reason = f"{len(highs)} stacks de uso alto para um teto de {cap}"
            logger.info(
                "rebalance: máquina %s (%s) acima do teto de heavy — %s",
                machine.get("name") or machine["id"], plan, reason,
            )
            for stack in highs[:excess]:
                if await relocate_stack_for_balance(stack, reason):
                    continue
                # Sem destino running com vaga de heavy: cascata igual à do
                # caminho de request — despausa uma parada; se não houver
                # nenhuma, provisiona. pause_when_healthy=False porque a
                # máquina nasce pra receber esta stack, não pro pool.
                # As travas de custo (interruptor auto_provision_enabled,
                # cooldown e lock por plano) vivem dentro dessas funções.
                outcome = await wake_some_machine_for_plan(plan)
                if outcome == "none":
                    await _try_provision_machine_for_plan(
                        plan,
                        f"rebalanceamento de uso alto: {reason}",
                        pause_when_healthy=False,
                    )
                retry_needed = True
                try:
                    await supa.log_machine_event(
                        machine["id"], "rebalance_pending",
                        f"Stack de uso alto aguardando máquina com vaga ({reason})",
                    )
                except Exception:
                    pass
                break  # sem destino agora, os demais excedentes também não teriam
    if retry_needed and retry_budget > 0:
        spawn_tracked(_rebalance_after_delay(retry_budget - 1))


async def _rebalance_after_delay(retry_budget: int) -> None:
    """Nova passada de rebalanceamento depois que a máquina despausada/criada
    teve tempo de subir. Sem isto, um desbalanceamento que disparou a cascata
    só seria resolvido no próximo ciclo de classificação (6h) — a máquina
    subiria e ficaria ociosa até lá.

    retry_budget decresce a cada elo da cadeia (ver HIGH_CAP_MAX_RETRIES):
    quando não há destino possível, a cadeia termina em vez de se reagendar
    indefinidamente."""
    await asyncio.sleep(HIGH_CAP_RETRY_DELAY_S)
    try:
        await rebalance_high_caps_once(retry_budget)
    except Exception as e:
        logger.warning("rebalanceamento (nova tentativa) falhou: %s", e)


async def usage_class_loop(interval_s: float = USAGE_CLASS_INTERVAL_S):
    # primeira rodada logo após o boot (delay curto só pra não competir com o
    # startup): com sleep-first de 6h e redeploy a cada push na main, um
    # processo que nunca fica 6h de pé jamais classificaria stack nenhuma —
    # a classificação inteira virava no-op silencioso
    await asyncio.sleep(60.0)
    while True:
        try:
            changed = await classify_stacks_once()
        except Exception as e:
            logger.warning("classificação periódica de usage_class falhou: %s", e)
            changed = 0
        try:
            # roda mesmo com changed == 0: o teto também é estourado por
            # caminhos que não passam pela classificação (migrateStack do
            # painel, realocação de emergência com fail-open por RPC ausente)
            await rebalance_high_caps_once()
        except Exception as e:
            logger.warning("rebalanceamento de teto de heavy falhou: %s", e)
        await asyncio.sleep(interval_s)


async def billing_reconcile_once() -> tuple[int, int]:
    """Materializa o corte por inadimplência e devolve as vagas de quem foi
    cortado. Devolve (suspensas, vagas_liberadas).

    O bloqueio em si NÃO depende deste loop — `authenticate` avalia
    billing_blocked por request, então o corte acontece na hora exata mesmo
    que isto aqui esteja parado. O loop existe por dois motivos diferentes:

      1. Sem ele, uma stack ficaria eternamente em 'past_due' com a
         tolerância vencida: bloqueada de fato, mas descrita como "em atraso"
         para o painel, o suporte e qualquer relatório. O estado do banco
         mentiria sobre o comportamento do sistema.
      2. Stack cortada continua contando em machine_stack_load — ocupando um
         slot pago que ninguém pode usar. Liberar a vaga é contábil, mesmo
         movimento do idle reaper de stacks base.
    """
    cutoff_iso = (
        datetime.now(timezone.utc) - timedelta(hours=BILLING_GRACE_HOURS)
    ).isoformat()

    suspended = 0
    try:
        expired = await supa.list_expired_grace_stacks(cutoff_iso)
    except Exception as e:
        logger.warning("reconciliação de billing: falha ao listar em atraso (%s)", e)
        expired = []

    for stack in expired:
        stack_id = stack["id"]
        try:
            if await supa.suspend_stack(stack_id):
                suspended += 1
                # o key_cache guarda o dict da stack inteiro (billing_status
                # incluso): sem evictar, a entrada velha continuaria dizendo
                # 'past_due' pelo resto do TTL
                _evict_key_cache_for_stack(stack_id)
                logger.info(
                    "billing: stack %s suspensa (em atraso desde %s)",
                    stack.get("slug") or stack_id, stack.get("past_due_since"),
                )
        except Exception as e:
            logger.warning("billing: suspender stack %s falhou (%s)", stack_id, e)

    released = 0
    try:
        blocked = await supa.list_blocked_stacks_with_machine()
    except Exception as e:
        logger.warning("reconciliação de billing: falha ao listar cortadas (%s)", e)
        blocked = []

    for stack in blocked:
        stack_id, machine_id = stack["id"], stack["machine_id"]
        # stack cortada não gera tráfego novo, mas um stream aberto ANTES do
        # corte pode seguir em voo — mesma guarda do idle reaper
        if in_flight.get((stack_id, machine_id), 0) > 0:
            continue
        try:
            if await supa.release_base_stack(stack_id, machine_id):
                released += 1
                _evict_key_cache_for_stack(stack_id)
                logger.info(
                    "billing: vaga de %s liberada em %s (assinatura cortada)",
                    stack.get("slug") or stack_id, machine_id,
                )
        except Exception as e:
            logger.warning("billing: liberar vaga de %s falhou (%s)", stack_id, e)

    return suspended, released


async def billing_reconcile_loop(interval_s: float = 60.0):
    while True:
        await asyncio.sleep(interval_s)
        try:
            await billing_reconcile_once()
        except Exception as e:
            logger.warning("reconciliação de billing: ciclo falhou (%s)", e)


async def reconcile_stale_routes_once() -> None:
    """Recupera contas presas em routing_state.lora_status in
    ('loading','migrating') há mais tempo do que qualquer load/migração
    legítima levaria. claim_route/set_client_location só saem desses
    estados via código explícito de reversão (do_load falhar → mark_slot_idle;
    migrate falhar → volta pra 'loaded') — se ESSA própria chamada de
    reversão falhar (ex.: hiccup de rede pro Supabase bem nesse instante),
    a linha fica presa pra sempre: nada mais no sistema a revisita
    (list_idle_routes só olha 'loaded'), e todo request futuro da conta
    bate em wait_until_routed e recebe 503 "adapter carregando" sem nunca
    se recuperar sozinho. Reset pra 'unloaded' é seguro mesmo se a operação
    original eventualmente completasse tarde: o pior caso é um load/migração
    redundante no próximo request, não um estado inconsistente."""
    cutoff_iso = datetime.fromtimestamp(
        time.time() - STALE_ROUTE_THRESHOLD_S, tz=timezone.utc
    ).isoformat()
    try:
        stale = await store.list_stale_transitional_routes(cutoff_iso)
    except Exception as e:
        logger.warning("reconciliação de rotas presas: falha ao listar (%s)", e)
        return
    for route in stale:
        stack_id = route.get("stack_id")
        if not stack_id:
            continue
        try:
            await store.mark_slot_idle(stack_id)
            logger.warning(
                "reconciliação: stack %s presa em '%s' desde %s — liberada",
                stack_id, route.get("lora_status"), route.get("updated_at"),
            )
        except Exception as e:
            logger.warning("reconciliação de rotas presas: falha ao liberar %s (%s)", stack_id, e)


async def stale_route_reconciliation_loop(interval_s: float = STALE_ROUTE_CHECK_INTERVAL_S):
    while True:
        await asyncio.sleep(interval_s)
        try:
            await reconcile_stale_routes_once()
        except Exception as e:
            logger.warning("reconciliação periódica de rotas presas falhou: %s", e)


async def machine_free_slots(machine: dict) -> int:
    """Slots LoRA livres da máquina.

    Slots por VRAM via machine_lora_slots() (mesma fórmula do painel);
    MAX_LORAS_PER_MACHINE atua como teto (espelho do --max-loras do pod) e
    como fallback quando a capacidade é desconhecida (sem VRAM/template).
    """
    by_vram = await supa.machine_lora_slots(machine["id"])
    slots = MAX_LORAS_PER_MACHINE if by_vram is None else min(by_vram, MAX_LORAS_PER_MACHINE)
    used = await supa.count_active_routes(machine["id"])
    return slots - used


def _forget_machine_upserts(machine_id: str) -> None:
    """Invalida o cache de upserts da máquina — o pod reiniciou e o agent
    voltou sem nenhuma chave em memória."""
    for k in [k for k in agent_key_upserts if k[1] == machine_id]:
        agent_key_upserts.pop(k, None)


def handle_machine_running(machine_id: str) -> None:
    """Callback do reconcile do lifecycle: máquina observada como promovida a
    running (religada pelo console do RunPod, recreateMachine, etc.) — o pod
    reiniciou com o agent zerado, então invalida o cache de upserts e agenda
    o reenvio das chaves."""
    _forget_machine_upserts(machine_id)
    schedule_key_sync(machine_id)


async def ensure_key_on_machine(entry: dict, machine: dict) -> None:
    """Garante a chave da conta no agent do pod antes do proxy.

    O agent perde as chaves em memória a cada restart do pod (stop/start) e,
    no fluxo base, a conta pode ser servida por uma máquina onde a chave
    nunca foi sincronizada (a chave é vinculada à máquina do stack) — sem o
    upsert, o pod rejeitaria com 401. Enquanto o pod boota, o call_agent
    devolve o 503 padrão e o retry do cliente converge sozinho.

    Existe ainda uma janela em que o status já é "running" no banco mas o
    processo do agent dentro do pod ainda não terminou de montar as rotas
    /admin — nesse caso o agent responde 404, que o call_agent propagaria
    como 502 cru. Fazemos algumas tentativas curtas e, se persistir,
    convertemos para um 503 amigável (mesmo padrão de waking_503/etc)."""
    cache_key = (entry["key_hash"], machine["id"])
    if agent_key_upserts.get(cache_key, 0) > time.time():
        return
    body = {"keys": [{
        "key_hash": entry["key_hash"],
        "api_key_id": entry.get("api_key_id"),
        "key_prefix": entry["key_prefix"],
        "account_name": entry["account_name"],
        # identificador de isolamento do prefix cache no agent
        # (proxy_policy.salt_ident) — sem ele o salt do tenant cai no ramo
        # degradado `kh:` e o cache dele é invalidado
        "stack_id": entry.get("stack_id"),
        "expires_at": entry.get("expires_at"),
    }]}
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            await call_agent(machine, "/upsert-keys", body)
            break
        except HTTPException as e:
            if e.status_code != 502 or attempt == attempts:
                raise agent_starting_503() if e.status_code == 502 else e
            await asyncio.sleep(1.5 * attempt)
    agent_key_upserts[cache_key] = time.time() + UPSERT_CACHE_TTL_S


async def auto_provision_enabled() -> bool:
    """Interruptor liga/desliga do provisionamento automático (system_settings,
    controlado pelo painel). Cache curto em memória — mesmo padrão do
    key_cache, evita 1 round-trip ao Supabase por request na hot path sem
    deixar o toggle demorar minutos pra fazer efeito."""
    global auto_provision_cache
    now = time.time()
    if auto_provision_cache and auto_provision_cache[1] > now:
        return auto_provision_cache[0]
    try:
        value = await supa.get_setting("auto_provision_enabled", False)
    except Exception:
        value = False  # Supabase fora do ar: nunca provisiona por engano
    auto_provision_cache = (value, now + SETTINGS_CACHE_TTL_S)
    return value


async def wake_machine(machine: dict, reason: str) -> str:
    """Religa um pod pausado (startPod) e o devolve ao pool de roteamento.

    Retorna: 'woke' = startPod disparado agora; 'cooldown' = tentativa recente
    ainda no cooldown (não tenta de novo); 'no_gpu' = o host cedeu a GPU e o
    start é impossível até recriar o pod (o chamador dispara a recriação);
    'failed' = falha por outro motivo (ou sem runpod_client/pod).

    O touch de atividade vem ANTES do flip para running: sem ele, o
    last_activity_at velho faria a auto-pausa parar a máquina de novo no
    próximo ciclo, enquanto o vLLM ainda carrega o modelo."""
    if runpod_client is None or not machine.get("runpod_pod_id"):
        return "failed"
    now = time.time()
    if now - last_wake_attempt.get(machine["id"], 0) < WAKE_COOLDOWN_S:
        return "cooldown"
    # marca a tentativa antes do primeiro await — atômico dentro do event loop
    last_wake_attempt[machine["id"]] = now
    try:
        await runpod_client.start_pod(machine["runpod_pod_id"])
    except Exception as e:
        if is_no_gpu_error(e):
            # host cedeu a GPU do pod pausado — religar nunca vai funcionar, o
            # chamador precisa recriar o pod num host novo. Limpa o cooldown de
            # wake desta máquina: caso contrário, nos WAKE_COOLDOWN_S seguintes
            # novas tentativas retornariam "cooldown" → o loop reportaria
            # "waking" ("está subindo") em vez de recriar. A recriação tem o
            # próprio cooldown/trava (try_recreate_machine), então o wake não
            # deve segurar a máquina que precisa ser recriada.
            last_wake_attempt.pop(machine["id"], None)
            logger.warning(
                "auto-wake: %s sem GPU no host, requer recriação (%s)", machine["id"], e
            )
            return "no_gpu"
        logger.warning("auto-wake: startPod de %s falhou (%s)", machine["id"], e)
        return "failed"
    try:
        await supa.touch_machine_activity(machine["id"])
    except Exception:
        pass
    await supa.set_machine_status(machine["id"], "running")
    # o pod reinicia com o agent zerado — invalida o cache de upserts e
    # agenda o reenvio das chaves assim que o vLLM ficar de pé
    _forget_machine_upserts(machine["id"])
    schedule_key_sync(machine["id"])
    try:
        await supa.log_machine_event(machine["id"], "started", f"Auto-wake: {reason}")
    except Exception:
        pass
    logger.info(
        "auto-wake: máquina %s (pod %s) religada — %s",
        machine["id"], machine["runpod_pod_id"], reason,
    )
    return "woke"


async def wake_some_machine_for_plan(plan: str) -> str:
    """Tenta pôr de pé alguma máquina pausada do template do plano. Só é chamado
    quando não há NENHUMA máquina running com vaga.

    Cascata por máquina pausada: religa (startPod); se o host cedeu a GPU
    ('no_gpu'), dispara a recriação num host novo. Retorna:
      - 'woke'      : despausou uma agora → cliente reintenta (religando);
      - 'recreating': nenhuma religou, mas disparamos/já há uma recriação →
                      cliente reintenta (recriando);
      - 'waking'    : há pausada subindo (cooldown de um wake recente bem
                      encaminhado), cliente reintenta;
      - 'none'      : não há pausada nenhuma (chamador decide provisionar)."""
    stopped = await supa.list_stopped_machines_for_plan(plan)
    if not stopped:
        return "none"
    recreating = False
    waking = False
    for m in stopped:
        if lock_active(recreating_in_progress, m["id"], RECREATE_LOCK_TTL_S):
            recreating = True
            continue
        outcome = await wake_machine(m, "requisição recebida sem máquina disponível")
        if outcome == "woke":
            return "woke"
        if outcome == "no_gpu":
            if await try_recreate_machine(m, "host sem GPU pra religar sob demanda"):
                recreating = True
        elif outcome == "cooldown":
            # tentativa recente; se não caiu em recreate, tratamos como subindo
            waking = True
    if recreating:
        return "recreating"
    return "waking" if waking else "none"


def waking_503() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="Sua máquina está sendo iniciada e ficará pronta em instantes. "
        "Tente novamente em alguns segundos.",
        headers={"Retry-After": "60"},
    )


def provisioning_503() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="Estamos preparando uma máquina nova para você — ficará pronta em "
        "instantes. Tente novamente em alguns segundos.",
        headers={"Retry-After": str(int(PROVISION_RETRY_AFTER_S))},
    )


def recreating_503() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="Estamos recriando sua máquina e ela ficará pronta em instantes. "
        "Tente novamente em alguns segundos.",
        headers={"Retry-After": str(int(RECREATE_RETRY_AFTER_S))},
    )


def preparing_503() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="Estamos preparando sua máquina — ela ficará disponível em instantes. "
        "Tente novamente em alguns segundos.",
        headers={"Retry-After": "30"},
    )


def agent_starting_503() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail="O serviço está iniciando e ficará pronto em instantes. "
        "Tente novamente em alguns segundos.",
        headers={"Retry-After": "15"},
    )


def capacity_503(plan: str, reason: str) -> HTTPException:
    """Sem vaga em nenhuma máquina do plano (todas cheias, ou nenhuma no ar) e
    nada a religar/provisionar. Ao contrário de waking/provisioning/recreating,
    aqui não há infraestrutura subindo — é volume de requests concorrentes
    excedendo a capacidade contratada. Logamos explicitamente porque esse 503
    nunca chega a gateway_requests (pick_machine_with_free_slot falha antes de
    existir machine_id/flight_key pra logar) — sem esta linha, a única pista
    fica no corpo da resposta que o cliente recebeu."""
    logger.warning(
        "capacidade: 503 no plano %s (%s) — provável excesso de requests "
        "concorrentes; sem máquina livre e nada a religar/provisionar",
        plan, reason,
    )
    return HTTPException(
        status_code=503,
        detail=f"Sem capacidade disponível no momento ({reason}). "
        "Isso costuma acontecer quando muitas requisições chegam ao mesmo "
        "tempo. Tente novamente em alguns segundos.",
        headers={"Retry-After": "5"},
    )


async def provision_machine_for_plan(plan: str) -> dict | None:
    """POST {PANEL_URL}/api/machines/provision — pede ao painel Next.js pra
    criar uma máquina nova do plano (o gateway nunca fala com a API de
    criação da RunPod diretamente, ver comentário das env vars no topo).
    None em qualquer falha (painel desligado/fora do ar, timeout, painel
    recusou) — o chamador decide o fallback, nunca propaga exceção."""
    if not PANEL_URL or not PANEL_ADMIN_SECRET:
        return None
    try:
        r = await panel_client.post(
            f"{PANEL_URL}/api/machines/provision",
            json={"plan": plan},
            headers={"X-Admin-Secret": PANEL_ADMIN_SECRET},
        )
    except httpx.HTTPError as e:
        logger.warning("provisionamento: chamada ao painel (%s) falhou (%s)", plan, e)
        return None
    if r.status_code != 200:
        logger.warning(
            "provisionamento: painel recusou %s (%s): %s", plan, r.status_code, r.text
        )
        return None
    return r.json()


async def recreate_machine_via_panel(machine_id: str) -> dict | None:
    """POST {PANEL_URL}/api/machines/{id}/recreate — pede ao painel pra recriar
    o pod num host novo (delete + create + start), mantendo a MESMA row de
    machines (stacks/chaves seguem apontando pra ela). Usado quando o auto-wake
    falhou por 'not enough free GPUs'. None em qualquer falha (painel desligado/
    fora do ar, timeout, recusa) — o chamador decide o fallback."""
    if not PANEL_URL or not PANEL_ADMIN_SECRET:
        return None
    try:
        r = await panel_client.post(
            f"{PANEL_URL}/api/machines/{machine_id}/recreate",
            headers={"X-Admin-Secret": PANEL_ADMIN_SECRET},
        )
    except httpx.HTTPError as e:
        logger.warning("recriação: chamada ao painel (%s) falhou (%s)", machine_id, e)
        return None
    if r.status_code != 200:
        logger.warning(
            "recriação: painel recusou %s (%s): %s", machine_id, r.status_code, r.text
        )
        return None
    return r.json()


async def _recreate_and_track(machine_id: str, reason: str) -> None:
    """Task de background: recria o pod e libera a trava ao fim. O request que
    disparou já respondeu 503 + Retry-After; o cliente reconverge quando o pod
    novo sobe (a reconciliação do gateway reenvia as chaves ao ficar running)."""
    try:
        result = await recreate_machine_via_panel(machine_id)
        if result is None:
            logger.warning(
                "recriação de %s não completou (%s) — fica na fila pro lifecycle retentar",
                machine_id, reason,
            )
        else:
            pending_recreates.discard(machine_id)  # sucesso: sai da fila de retry
            logger.info("recriação de %s disparada — %s", machine_id, reason)
    finally:
        recreating_in_progress.pop(machine_id, None)


async def try_recreate_machine(machine: dict, reason: str) -> bool:
    """Dispara a recriação em background se o painel estiver configurado, não
    houver uma recriação em andamento pra essa máquina e o cooldown já tiver
    passado. Retorna True se há recriação encaminhada (disparada agora, já em
    andamento, ou recente dentro do cooldown) — o chamador levanta
    recreating_503(). Não fica atrás de auto_provision_enabled: recriar restaura
    uma máquina que o usuário já provisionou (o host cedeu a GPU), não cria
    capacidade nova.

    Enfileira a máquina em pending_recreates: se a chamada ao painel falhar (ou
    o processo cair antes de concluir), o lifecycle loop retenta. A entrada só
    sai da fila quando uma recriação conclui com sucesso."""
    machine_id = machine["id"]
    if not PANEL_URL or not PANEL_ADMIN_SECRET:
        return False
    pending_recreates.add(machine_id)
    if lock_active(recreating_in_progress, machine_id, RECREATE_LOCK_TTL_S):
        return True
    now = time.time()
    if now - last_recreate_attempt.get(machine_id, 0) < RECREATE_COOLDOWN_S:
        # recriação recente já disparada — o pod novo está subindo
        return True
    # checagem + marcação sem await no meio (atômicas dentro do event loop,
    # mesma disciplina do provisioning_in_progress)
    last_recreate_attempt[machine_id] = now
    recreating_in_progress[machine_id] = now
    spawn_tracked(_recreate_and_track(machine_id, reason))
    return True


async def _wait_machine_healthy(
    machine_id: str, timeout_s: float, poll_interval_s: float = 10.0
) -> bool:
    """Poll em GET {public_url}/health (sem auth — endpoint do agent) até o
    vLLM confirmar modelo carregado (vllm_ready) ou o timeout esgotar."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        machine = await supa.get_machine(machine_id)
        if machine and machine.get("public_url"):
            try:
                r = await proxy_client.get(
                    f"{machine['public_url']}/health",
                    timeout=httpx.Timeout(5.0, connect=5.0),
                )
                if r.status_code == 200 and r.json().get("vllm_ready"):
                    return True
            except Exception:
                pass
        await asyncio.sleep(poll_interval_s)
    return False


def schedule_key_sync(machine_id: str) -> None:
    """Agenda (fire-and-forget) o reenvio das chaves da máquina ao agent
    assim que o pod ficar saudável — o agent volta de qualquer restart com
    zero chaves em memória; sem isso, todo request pós-religada vira 401 até
    um sync manual. Checagem + marcação sem await no meio (atômicas dentro
    do event loop, mesma disciplina do provisioning_in_progress)."""
    if lock_active(key_sync_in_progress, machine_id, KEY_SYNC_LOCK_TTL_S):
        return
    key_sync_in_progress[machine_id] = time.time()
    spawn_tracked(_sync_keys_when_healthy(machine_id))


async def _sync_keys_when_healthy(machine_id: str) -> None:
    """Task de background: espera o vLLM de pé e reenvia em lote todas as
    chaves ativas da máquina. Usa /upsert-keys (não /sync-keys) pra nunca
    clobber chaves que o fluxo LoRA/base upsertou enquanto o lote esperava.
    Nunca deixa exceção escapar (fire-and-forget)."""
    try:
        healthy = await _wait_machine_healthy(
            machine_id, MACHINE_HEALTH_TIMEOUT_S, MACHINE_HEALTH_POLL_INTERVAL_S
        )
        if not healthy:
            logger.warning("key-sync: máquina %s não ficou saudável a tempo", machine_id)
            return
        machine = await supa.get_machine(machine_id)
        if not machine or not machine.get("public_url"):
            return
        keys = await supa.list_active_keys_for_machine(machine_id)
        if keys:
            await call_agent(machine, "/upsert-keys", {"keys": keys})
        try:
            await supa.log_machine_event(
                machine_id, "sync", f"{len(keys)} chave(s) reenviada(s) após religar"
            )
        except Exception:
            pass
        logger.info("key-sync: %d chave(s) reenviada(s) para %s", len(keys), machine_id)
    except Exception as e:
        logger.warning("key-sync: reenvio de chaves para %s falhou (%s)", machine_id, e)
    finally:
        key_sync_in_progress.pop(machine_id, None)


async def _provision_and_track(plan: str, reason: str, pause_when_healthy: bool) -> None:
    """Task de background: cria -> espera saudável -> opcionalmente pausa
    (reposição proativa) — nunca deixa exceção escapar (fire-and-forget)."""
    try:
        machine = await provision_machine_for_plan(plan)
        if not machine:
            return
        try:
            await supa.log_machine_event(
                machine["machine_id"], "created", f"Provisionamento automático: {reason}"
            )
        except Exception:
            pass
        healthy = await _wait_machine_healthy(
            machine["machine_id"], MACHINE_HEALTH_TIMEOUT_S, MACHINE_HEALTH_POLL_INTERVAL_S
        )
        if healthy and pause_when_healthy and runpod_client is not None:
            m = await supa.get_machine(machine["machine_id"])
            if m and m.get("runpod_pod_id"):
                try:
                    await runpod_client.stop_pod(m["runpod_pod_id"])
                    await supa.set_machine_status(machine["machine_id"], "stopped")
                    await supa.log_machine_event(
                        machine["machine_id"], "stopped",
                        "Reposição proativa: pausada assim que ficou saudável",
                    )
                except Exception as e:
                    logger.warning(
                        "provisionamento: pausa pós-boot de %s falhou (%s)",
                        machine["machine_id"], e,
                    )
    except Exception as e:
        logger.warning("provisionamento automático (%s) falhou (%s)", plan, e)
    finally:
        provisioning_in_progress.pop(plan, None)


async def _try_provision_machine_for_plan(
    plan: str, reason: str, pause_when_healthy: bool
) -> bool:
    """Dispara a criação em background se o interruptor estiver ligado, o
    painel estiver configurado, não houver uma criação em andamento pro
    plano e o cooldown já tiver passado. Fonte única de verdade do
    interruptor — os 3 chamadores (cascata reativa x2, ensure_capacity_once)
    não precisam checar auto_provision_enabled() cada um por conta própria.
    A decisão de SE vale a pena criar (dado que está ligado) é toda do
    chamador — aqui só há as travas."""
    if not await auto_provision_enabled():
        return False
    if not PANEL_URL or not PANEL_ADMIN_SECRET:
        # sem painel configurado, provision_machine_for_plan sempre devolve
        # None — sem essa checagem aqui, o chamador levantaria um
        # provisioning_503() mentiroso (promete retry, mas nunca vai criar)
        return False
    if lock_active(provisioning_in_progress, plan, PROVISION_LOCK_TTL_S):
        return False
    now = time.time()
    if now - last_provision_attempt.get(plan, 0) < PROVISION_COOLDOWN_S:
        return False
    # daqui pra baixo não há mais nenhum await antes de marcar a trava —
    # checagem + marcação são atômicas dentro do event loop (mesmo cuidado
    # do wake_machine existente)
    last_provision_attempt[plan] = now
    provisioning_in_progress[plan] = now
    spawn_tracked(_provision_and_track(plan, reason, pause_when_healthy))
    return True


async def try_provision_for_request(plan: str, reason: str) -> bool:
    """Cascata reativa (3º nível): não pausa ao ficar saudável — o próprio
    request que disparou precisa da máquina de pé pro retry."""
    return await _try_provision_machine_for_plan(plan, reason, pause_when_healthy=False)


async def try_provision_for_pool(plan: str, reason: str) -> bool:
    """Reposição proativa: pausa ao ficar saudável — ninguém está esperando,
    minimiza custo de GPU ociosa."""
    return await _try_provision_machine_for_plan(plan, reason, pause_when_healthy=True)


async def pick_machine_with_free_slot(plan: str) -> dict:
    """Alocação placeholder: primeira máquina running (do template do plano
    da conta) com slot LoRA livre. Sem capacidade → tenta religar um pod
    pausado do plano (auto-wake); sem pausada, tenta provisionar uma nova
    (3º nível, se o interruptor estiver ligado) antes de desistir."""
    machines = await supa.list_running_machines_for_plan(plan)
    for m in machines:
        if await machine_free_slots(m) > 0:
            return m
    woke = await wake_some_machine_for_plan(plan)
    if woke == "recreating":
        raise recreating_503()
    if woke != "none":
        raise waking_503()
    if lock_active(
        provisioning_in_progress, plan, PROVISION_LOCK_TTL_S
    ) or await try_provision_for_request(plan, "sem máquina com vaga nem pausada"):
        raise provisioning_503()
    if not machines:
        raise capacity_503(plan, "nenhuma máquina disponível")
    raise capacity_503(plan, "todas as máquinas estão cheias")


async def do_load(stack_id: str, entry: dict, machine: dict, adapter: dict) -> None:
    """Garante a chave no agent, baixa+carrega o adapter e confirma a rota.
    Escopo por stack: o adapter é nomeado e roteado por stack_id (a chave, que é
    por conta, é garantida separadamente por ensure_key_on_machine)."""
    await ensure_key_on_machine(entry, machine)
    files = await supa.signed_lora_files(adapter["storage_path"])
    await call_agent(
        machine, "/load-lora",
        {"lora_name": lora_name(stack_id), "files": files},
        timeout_s=LORA_LOAD_TIMEOUT_S,
    )
    await store.set_client_location(
        stack_id,
        machine_id=machine["id"],
        lora_adapter_id=adapter["id"],
        lora_status="loaded",
    )


async def wait_until_routed(stack_id: str) -> dict | None:
    """Espera (poll curto) um load em andamento de outro request terminar."""
    deadline = time.time() + LOAD_WAIT_TIMEOUT_S
    while time.time() < deadline:
        await asyncio.sleep(1.0)
        route = await store.get_client_location(stack_id)
        if route and route["lora_status"] in ("loaded", "migrating") and route["machine_id"]:
            return route
        if not route or route["lora_status"] == "unloaded":
            return None  # o load falhou e o slot foi liberado
    return None


def resolve_key_stack(entry: dict) -> tuple[dict | None, str | None]:
    """Stack efetiva da chave e o plano dela pro resto do fluxo.

    Plano é propriedade da stack (migration 0027 removeu accounts.plan — uma
    conta pode ter stacks de planos diferentes, então não existe mais "o
    plano da conta"). Chave sem `stack_id` resolvível só passa por
    `authenticate` quando a conta não tem NENHUMA stack — nesse caso não há
    plano nenhum pra usar; devolve (None, None) e quem chama trata como
    "conta sem stack configurada" em vez de adivinhar."""
    stack_id = entry.get("stack_id")
    if stack_id:
        stack = next((s for s in entry.get("stacks") or [] if s["id"] == stack_id), None)
        if stack:
            return stack, stack["plan"]
    return None, None


def apply_stack_sampling_defaults(body_json: dict, entry: dict) -> None:
    """Aplica default_temperature/default_top_p da stack (migration 0035)
    quando o cliente não mandou o parâmetro. Roda ANTES do clamp de
    segurança em validate_body/validate_responses_body, que cobre tanto o
    valor do cliente quanto o default recém-aplicado."""
    stack, _ = resolve_key_stack(entry)
    if not stack:
        return
    if "temperature" not in body_json and stack.get("default_temperature") is not None:
        body_json["temperature"] = stack["default_temperature"]
    if "top_p" not in body_json and stack.get("default_top_p") is not None:
        body_json["top_p"] = stack["default_top_p"]


async def machine_admits(machine_id: str, usage_class: str = "low") -> bool:
    """A máquina aceita mais uma stack da classe dada? Duas restrições
    INDEPENDENTES (migration 0037):

      1. cabeça livre: machine_stack_load < machine_stack_slots. Vale igual
         para qualquer classe — 18 slots comportam 18 contas, seja qual for a
         mistura. (Era isto que a ocupação ponderada da 0032 quebrava: um
         high comia 3 cabeças e a máquina "enchia" com 6 clientes.)
      2. teto de mistura: só stacks 'high' são limitadas, por
         machine_high_cap. É SUB-TETO, não reserva — os lugares de heavy não
         ficam parados quando não há heavy, então 1 high + 17 low/medium numa
         máquina de 18 é perfeitamente válido.

    Capacidade desconhecida/sem teto (slots 0 ou None) é aceita, mesmo
    critério do allocateMachineForTemplate do painel (lib/actions.ts)."""
    slots = await supa.machine_stack_slots(machine_id)
    if slots:  # 0 e None = capacidade desconhecida/sem teto
        if await supa.machine_stack_load(machine_id) >= slots:
            return False
    if usage_class != "high":
        return True
    cap = await supa.machine_high_cap(machine_id)
    if cap is None:
        return True  # template sem teto configurado: fail-open
    return await supa.machine_high_count(machine_id) < cap


async def pick_running_machine_with_stack_slot(
    plan: str, exclude_machine_id: str | None = None, usage_class: str = "low"
) -> dict | None:
    """Primeira máquina running do plano que admite uma stack da classe dada
    (ver machine_admits para as duas restrições)."""
    for m in await supa.list_running_machines_for_plan(plan):
        if exclude_machine_id and m["id"] == exclude_machine_id:
            continue
        if await machine_admits(m["id"], usage_class):
            return m
    return None


async def reallocate_stack(entry: dict, stack: dict, old_machine: dict) -> dict | None:
    """Realocação automática (cenário: máquina do stack pausada/terminada e
    o usuário mandou request): muda a "casa" do usuário DE VEZ pra uma
    running com vaga — stacks.machine_id reponta e as chaves ativas da conta
    MOVEM junto (api_keys.machine_id); a plain key do cliente continua a
    mesma, diferente do migrateStack do painel, que cria/revoga chaves.
    None = sem vaga em lugar nenhum ou perdeu a corrida (o chamador decide:
    religar a própria máquina ou cair no fallback por plano).

    Limitação aceita: com múltiplos stacks da conta na mesma origem, só o
    stack escolhido reponta (as chaves movem juntas); os irmãos ficam para o
    admin migrar via migrateStack.
    """
    moved = False
    async with realloc_locks[stack["plan"]]:
        fresh = await supa.get_stack(stack["id"])
        if not fresh:
            return None
        if fresh["machine_id"] != old_machine["id"]:
            # request concorrente já realocou — segue a máquina nova dele
            if not fresh["machine_id"]:
                return None
            m = await supa.get_machine(fresh["machine_id"])
            if not (m and m.get("status") == "running" and m.get("public_url")):
                return None
            target = m
        else:
            target = await pick_running_machine_with_stack_slot(
                stack["plan"],
                exclude_machine_id=old_machine["id"],
                # o destino precisa ter cabeça livre E, se esta stack for
                # high, espaço no teto de heavy — senão a realocação de
                # emergência (máquina pausada) resolveria a indisponibilidade
                # às custas de criar um pod desbalanceado
                usage_class=fresh.get("usage_class") or "low",
            )
            if not target:
                return None
            if not await supa.repoint_stack(stack["id"], old_machine["id"], target["id"]):
                return None
            await supa.move_account_keys(
                entry["account_id"], old_machine["id"], target["id"],
                stack_id=stack["id"] if entry.get("stack_id") else None,
            )
            moved = True

    # stack é o mesmo objeto guardado no key_cache — mutar in place mantém o
    # cache coerente pelo resto do TTL sem flush
    stack["machine_id"] = target["id"]
    agent_key_upserts.pop((entry["key_hash"], old_machine["id"]), None)
    await ensure_key_on_machine(entry, target)
    if moved:
        try:
            await store.record_reallocation(
                entry["account_id"],
                from_machine_id=old_machine["id"],
                machine_id=target["id"],
            )
            reason = {"stopped": "pausada", "terminated": "terminada"}.get(
                old_machine.get("status"), "indisponível"
            )
            await supa.log_machine_event(
                target["id"], "stack_migrated",
                f"Stack {stack.get('slug') or stack['id']} realocada automaticamente "
                f"({old_machine.get('name') or 'origem'} {reason})",
            )
        except Exception:
            pass  # histórico é best-effort, nunca derruba o request
        logger.info(
            "realloc: stack %s da conta %s movida de %s para %s",
            stack.get("slug") or stack["id"], entry["account_id"],
            old_machine["id"], target["id"],
        )
    return target


async def place_base_stack(entry: dict, stack: dict) -> dict | None:
    """Re-aloca a "casa" de uma stack de modelo base que teve o slot liberado
    por ociosidade (stacks.machine_id == NULL, zerado pelo reap_idle_base_stacks
    do lifecycle). Escolhe uma máquina running do MESMO plano com vaga
    PONDERADA pela classe de uso (baixo/médio/alto), não necessariamente a
    anterior. Irmão enxuto do reallocate_stack, sem origem conhecida.

    None = sem vaga em máquina nenhuma (o chamador cai no wake/provision por
    plano) ou perdeu a corrida sem a máquina do vencedor estar pronta.
    """
    async with realloc_locks[stack["plan"]]:
        fresh = await supa.get_stack(stack["id"])
        if not fresh:
            return None
        if fresh.get("machine_id"):
            # request concorrente já re-homeou — segue a máquina nova dele
            m = await supa.get_machine(fresh["machine_id"])
            if m and m.get("status") == "running" and m.get("public_url"):
                stack["machine_id"] = m["id"]
                await ensure_key_on_machine(entry, m)
                return m
            return None
        target = await pick_running_machine_with_stack_slot(
            stack["plan"],
            usage_class=fresh.get("usage_class") or "low",
        )
        if not target:
            return None
        if not await supa.repoint_stack_from_null(stack["id"], target["id"]):
            return None
        await supa.rebind_stack_keys(entry["account_id"], target["id"], stack["id"])

    # stack é o mesmo objeto do key_cache — mutar in place mantém o cache
    # coerente pelo resto do TTL sem flush (igual ao reallocate_stack)
    stack["machine_id"] = target["id"]
    await ensure_key_on_machine(entry, target)
    try:
        await supa.log_machine_event(
            target["id"], "stack_placed",
            f"Stack {stack.get('slug') or stack['id']} re-alocada após ociosidade",
        )
    except Exception:
        pass  # histórico é best-effort, nunca derruba o request
    logger.info(
        "place_base_stack: stack %s da conta %s re-alocada em %s",
        stack.get("slug") or stack["id"], entry["account_id"], target["id"],
    )
    return target


async def resolve_base_machine(account_id: str, entry: dict) -> tuple[dict, str]:
    """Máquina pro modelo base (conta sem adapter), stack-aware. Retorna
    (machine, effective_plan) — effective_plan é sempre o plano da STACK
    resolvida pela chave (ver resolve_key_stack; chamador já garantiu que
    não é None antes de chegar aqui).

    1. Máquina do stack running → serve nela (chave garantida via upsert
       lazy — o pod pode ter reiniciado e perdido as chaves).
    2. Pausada/terminada/erro → realoca o stack pra outra running com vaga
       (permanente). Sem vaga e pausada → religa a PRÓPRIA máquina do
       usuário (as chaves já estão vinculadas a ela) e responde 503 +
       Retry-After pro retry do cliente.
    3. Sem stack, stack sem máquina, ou wake da própria falhou (ex.: host
       sem GPU livre) → fallback por plano (comportamento original), agora
       com upsert lazy da chave — sem ele o pod da outra máquina rejeitaria
       a chave com 401.
    """
    stack, effective_plan = resolve_key_stack(entry)
    if stack and stack.get("machine_id"):
        machine = await supa.get_machine(stack["machine_id"])
        if machine:
            status = machine.get("status")
            if status == "running" and machine.get("public_url"):
                await ensure_key_on_machine(entry, machine)
                return machine, effective_plan
            if status in ("stopped", "terminated", "error"):
                target = await reallocate_stack(entry, stack, machine)
                if target:
                    return target, effective_plan
                if status == "stopped":
                    slug = stack.get("slug") or stack["id"]
                    if lock_active(recreating_in_progress, machine["id"], RECREATE_LOCK_TTL_S):
                        # recriação disparada por um request anterior ainda em
                        # curso — o pod novo está subindo
                        raise recreating_503()
                    outcome = await wake_machine(
                        machine, f"stack {slug}: máquina pausada e sem vaga nas demais"
                    )
                    if outcome in ("woke", "cooldown"):
                        # 'cooldown' = request concorrente já disparou o wake e o
                        # pod está subindo — não religa uma 2ª máquina à toa
                        raise waking_503()
                    fresh = await supa.get_machine(machine["id"])
                    if fresh and fresh.get("status") == "running":
                        raise waking_503()
                    if outcome == "no_gpu" and await try_recreate_machine(
                        machine, f"stack {slug}: host sem GPU pra religar"
                    ):
                        # host cedeu a GPU do pod pausado → recria num host novo
                        raise recreating_503()
                    # wake e recreate não resolveram → fallback por plano:
                    # serve temporário sem mover o stack
    elif stack and not stack.get("machine_id"):
        # casa liberada por ociosidade (reap_idle_base_stacks zerou machine_id)
        # ou stack nunca homeada → re-aloca DE VEZ numa máquina do plano com
        # vaga ponderada, em vez de cair no fallback "primeira máquina" (que
        # servia sem contabilizar a vaga e sem re-homear)
        target = await place_base_stack(entry, stack)
        if target:
            return target, effective_plan
        # sem vaga em lugar nenhum → cai no fallback (wake/provision por plano)

    machines = await supa.list_running_machines_for_plan(effective_plan)
    if not machines:
        woke = await wake_some_machine_for_plan(effective_plan)
        if woke == "recreating":
            raise recreating_503()  # host sem GPU → recriando num host novo
        if woke != "none":
            raise waking_503()  # despausando agora ou uma já está subindo
        if lock_active(
            provisioning_in_progress, effective_plan, PROVISION_LOCK_TTL_S
        ) or await try_provision_for_request(
            effective_plan, "sem máquina para o modelo base"
        ):
            raise provisioning_503()
        raise preparing_503()
    machine = machines[0]
    await ensure_key_on_machine(entry, machine)
    return machine, effective_plan


async def resolve_machine_readonly(entry: dict) -> dict | None:
    """Máquina que ATUALMENTE serve a stack da chave, ou None — sem alocar nada.

    Existe porque resolve_route não serve pra endpoint de metadado: ele aloca
    máquina, espera lora_status "loading" (wait_until_routed) e pode acordar
    pod. O /v1/messages/count_tokens é chamado pelo cliente a cada turno pra
    decidir se compacta; fazê-lo acordar pod seria absurdo. Aqui é só leitura:
    rota existente + máquina viva, ou None.

    None é sempre aceitável pra quem chama (cai na heurística), então qualquer
    falha de rede vira None em vez de derrubar a request."""
    stack, _plan = resolve_key_stack(entry)
    if not stack:
        return None
    try:
        route = await store.get_client_location(stack["id"])
        if not route or not route.get("machine_id"):
            return None
        machine = await supa.get_machine(route["machine_id"])
    except httpx.HTTPError:
        return None
    if not machine or machine.get("status") != "running" or not machine.get("public_url"):
        return None
    return machine


async def resolve_route(account_id: str, entry: dict) -> tuple[dict, bool, str, str]:
    """Resolve (machine, rewrite_model, effective_plan, stack_id) para a conta.

    O roteamento é escopado por STACK (migration 0029): a rota, o nome do
    adapter e o in_flight/drain são por stack_id (resolvido da própria chave via
    resolve_key_stack). account_id ainda circula para chaves e histórico.

    Regra primária: rota com machine_id e status loaded/migrating → proxy
    direto (migrating = origem continua servindo). Sem adapter registrado →
    modelo base numa máquina running, sem reescrever "model".

    `effective_plan` vem sempre de `resolve_key_stack` — plano é propriedade
    da stack da própria chave (migration 0027 removeu accounts.plan), tanto
    nos branches de adapter LoRA quanto no modelo base. Adapter LoRA também
    é resolvido por stack (`latest_ready_adapter_for_stack`, migration 0026)
    — cada stack pode ter (ou não) seu próprio fine-tune.
    """
    stack, effective_plan = resolve_key_stack(entry)
    if effective_plan is None:
        raise HTTPException(status_code=503, detail="conta sem stack configurada")
    stack_id = stack["id"]

    route = await store.get_client_location(stack_id)

    if route and route["machine_id"] and route["lora_status"] in ("loaded", "migrating"):
        machine = await supa.get_machine(route["machine_id"])
        if not machine:
            raise HTTPException(status_code=503, detail="máquina da rota não existe mais")
        # "stopped": stop manual pelo painel com a rota ainda apontando pra
        # máquina (a auto-pausa exige 0 rotas, então nunca cria este estado
        # sozinha). "terminated"/"error": pod sumiu de vez (deletado via
        # console do RunPod, host reclamou a instância) —
        # reconcile_statuses_once promove pra esses status SEM checar rotas
        # ativas (ao contrário de stop_idle_machines_once). Sem public_url:
        # estado transitório/inconsistente, não dá pra servir mesmo com
        # status "running". Em qualquer um desses casos o adapter não está
        # mais carregado (nem nunca mais vai estar, nos dois primeiros) —
        # sem tratar isso aqui, a conta ficava presa apontando pra uma
        # máquina morta em todo request seguinte, sem nenhuma auto-cura.
        if machine.get("status") in ("stopped", "terminated", "error") or not machine.get(
            "public_url"
        ):
            await store.mark_slot_idle(stack_id)
            route = None
        else:
            return machine, True, effective_plan, stack_id

    if route and route["lora_status"] == "loading":
        waited = await wait_until_routed(stack_id)
        if waited:
            machine = await supa.get_machine(waited["machine_id"])
            if machine:
                return machine, True, effective_plan, stack_id
        raise HTTPException(
            status_code=503,
            detail="adapter carregando, tente novamente",
            headers={"Retry-After": "5"},
        )

    # sem rota ativa: a STACK da chave tem adapter?
    adapter = await supa.latest_ready_adapter_for_stack(stack_id)
    if not adapter:
        # sem adapter registrado → serve o modelo base, stack-aware: máquina
        # da stack da chave quando running; pausada → realocação automática
        # ou wake da própria; fallback por plano (Go nunca cai numa
        # máquina servindo o modelo do Pro/Max, e vice-versa)
        machine, effective_plan = await resolve_base_machine(account_id, entry)
        return machine, False, effective_plan, stack_id

    machine = await pick_machine_with_free_slot(effective_plan)
    result = await store.claim_client_location(stack_id, account_id, machine["id"])
    if not result["claimed"]:
        # outro request da mesma stack venceu a corrida — espera o load dele
        waited = await wait_until_routed(stack_id)
        if waited:
            m = await supa.get_machine(waited["machine_id"])
            if m:
                return m, True, effective_plan, stack_id
        raise HTTPException(
            status_code=503,
            detail="adapter carregando, tente novamente",
            headers={"Retry-After": "5"},
        )

    try:
        await do_load(stack_id, entry, machine, adapter)
    except HTTPException:
        await store.mark_slot_idle(stack_id)
        raise
    except Exception as e:
        await store.mark_slot_idle(stack_id)
        raise HTTPException(status_code=503, detail=f"falha ao carregar adapter: {e}")
    return machine, True, effective_plan, stack_id


# ---------- Proxy ----------


async def maybe_touch(stack_id: str, machine_id: str | None = None):
    now = time.time()
    if now - last_touch.get(stack_id, 0) >= TOUCH_THROTTLE_S:
        last_touch[stack_id] = now
        try:
            await store.touch(stack_id)
        except Exception:
            pass  # touch é best-effort, nunca derruba o request
    # atividade por máquina (base da auto-pausa) — cobre também requests de
    # modelo base sem rota, que não tocam routing_state
    if machine_id and now - last_machine_touch.get(machine_id, 0) >= TOUCH_THROTTLE_S:
        last_machine_touch[machine_id] = now
        try:
            await supa.touch_machine_activity(machine_id)
        except Exception:
            pass
    # atividade por stack (relógio de ociosidade do modelo base) — mantém a
    # stack "fresca" pra não ser reapada; vale pra base E LoRA, já que a stack
    # LoRA também tem stacks.machine_id como casa de fallback
    if now - last_stack_touch.get(stack_id, 0) >= TOUCH_THROTTLE_S:
        last_stack_touch[stack_id] = now
        try:
            await supa.touch_stack_activity(stack_id)
        except Exception:
            pass  # touch é best-effort, nunca derruba o request


async def embed_query(text: str) -> list[float] | None:
    """Embedding da última mensagem do usuário, pro retrieval do RAG.
    None em qualquer falha (sem OPENAI_API_KEY, API fora do ar, etc.) —
    RAG é best-effort, nunca derruba o request de inferência."""
    if not OPENAI_API_KEY:
        return None
    try:
        r = await openai_client.post(
            "/embeddings", json={"model": EMBEDDING_MODEL, "input": text}
        )
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]
    except Exception:
        return None


# MIN_MAX_TOKENS é importado de context_budget (RESERVED_OUTPUT_TOKENS) — mora
# lá porque é ele que calcula a janela de input recomendada pro cliente
# (usable_input_tokens/auto_compact_window) e é o único módulo importável sem as
# env vars obrigatórias daqui. Piso de max_tokens: thinking mode (Qwen3.x) corta
# o raciocínio no meio quando o cliente NÃO manda max_tokens algum — comum em
# ferramentas de terceiro (Cursor, Continue, Cline etc.) que nem passam esse
# parâmetro. Como o produto é BYOE (o usuário aponta a ferramenta dele direto
# pro endpoint, sem UI de chat própria controlando esse parâmetro), o gateway
# impõe o piso aqui pra garantir qualidade consistente independente do
# cliente.
#
# Quando o cliente MANDA um max_tokens baixo de propósito (ex.: orçamento de
# tempo apertado), promover pro piso silenciosamente o ignora por completo —
# medido ao vivo: max_tokens=100 pedido, 3690 gerado, sempre com stop_reason
# "end_turn" (o piso de 8000 dava folga de sobra pro thinking "pensar alto" e
# ainda terminar sozinho, bem antes do teto real). Nesse caso o comportamento
# correto é respeitar o valor pedido, mas desligar o thinking (mesmo padrão
# de /v1/documents/extract, ver "enable_thinking" abaixo) pra esse teto baixo
# não truncar o raciocínio no meio — ver validate_body().
MAX_MAX_TOKENS = int(os.environ.get("MAX_MAX_TOKENS", "16000"))  # teto: sem isso um
# cliente podia pedir max_tokens arbitrário e a GPU rodava até esgotar o contexto,
# sem nenhum controle de custo (ver também check_concurrency/RATE_LIMIT_RPM)

ALLOWED_ROLES = {"system", "user", "assistant", "tool"}


def effective_model_name(stack_id: str, rewrite_model: bool, machine: dict) -> str | None:
    """Nome com que o vLLM realmente vai servir esta requisição.

    Extraído de pin_model porque o log de requisições (gateway_requests)
    precisa do MESMO valor — e a primeira versão dele gravava `rewrite_model`,
    o booleano, fazendo a coluna `model` virar a string "false" na tela.
    Uma função só, dois consumidores: as duas não podem divergir de novo."""
    if rewrite_model:
        return lora_name(stack_id)
    return machine.get("served_model_name") or machine.get("model_name")


def pin_model(body_json: dict, stack_id: str, rewrite_model: bool, machine: dict) -> None:
    """Trava o campo "model": nunca confia no que o cliente mandou. Stack com
    adapter LoRA -> nome do adapter da PRÓPRIA stack (antes disso, além do
    cross-tenant, duas stacks da mesma conta colidiam no mesmo nome de adapter);
    stack base -> served_model_name da máquina (o alias de --served-model-name,
    ex.: "pro-base"). É o ÚNICO nome que o vLLM aceita quando o template define
    esse alias; fixar o machines.model_name (path do HF, ex.: "Qwen/...") daria
    404 "model does not exist". Fallback pro model_name quando o template não usa
    a flag (aí o vLLM serve pelo próprio --model).
    Roda sempre, mesmo se o cliente omitiu "model" ou mandou um --model
    diferente na CLI dele (Codex/Claude Code guardam isso em config local, que
    não temos como fiscalizar — a única trava confiável é no servidor)."""
    body_json["model"] = effective_model_name(stack_id, rewrite_model, machine)


async def validate_body(
    body_json: dict,
    entry: dict,
    rewrite_model: bool,
    machine: dict,
    stack_id: str,
    budget_out: dict | None = None,
) -> dict:
    """Ponto único de validação/transformação do corpo antes do proxy:
    trava o modelo, aplica piso/teto de max_tokens e clamp de parâmetros
    (qualquer endpoint /v1/*) e, só para chat completions (body com
    "messages"), filtra roles e injeta system prompt da stack + RAG.

    budget_out (opcional): recebe {"budget": PromptBudget} com o que o clamp
    dinâmico concluiu. Out-param em vez de mudar o tipo de retorno porque são
    3 chamadores e só um (o /v1/messages) precisa do dado — ele usa o
    est_tokens pra emitir usage.input_tokens no message_start, sem o qual o
    rastreador de contexto do Claude Code fica em zero e o auto-compact nunca
    dispara. Guardar no próprio body_json sob chave privada foi descartado: um
    pop esquecido mandaria campo desconhecido pro vLLM."""
    pin_model(body_json, stack_id, rewrite_model, machine)

    # vLLM só manda "usage" no chunk final do SSE quando o pedido inclui
    # stream_options.include_usage (spec OpenAI) — sem isso, tokens_in/
    # tokens_out ficam null em gateway_requests pra QUALQUER requisição
    # streaming cujo client não sete essa flag sozinho (achado com a chave
    # "playground": um teste manual não mandava o campo e a página de
    # Requisições ficava sem os tokens). Forçado aqui, ponto único de
    # validação do corpo, pra logar tokens sempre, independente do client.
    if body_json.get("stream") is True:
        stream_options = body_json.get("stream_options")
        if not isinstance(stream_options, dict):
            stream_options = {}
        stream_options["include_usage"] = True
        body_json["stream_options"] = stream_options

    current_max_tokens = body_json.get("max_tokens")
    if not isinstance(current_max_tokens, int):
        # cliente não mandou nada — piso de sempre, thinking fica ligado
        # (comportamento inalterado, ver comentário de MIN_MAX_TOKENS acima)
        body_json["max_tokens"] = MIN_MAX_TOKENS
    elif current_max_tokens < MIN_MAX_TOKENS:
        # cliente pediu um teto baixo de propósito — respeita o valor (não
        # promove mais pro piso) e desliga o thinking pra esse teto não
        # truncar o raciocínio no meio, mesmo padrão já usado em
        # /v1/documents/extract e /v1/documents/generate
        chat_kwargs = body_json.get("chat_template_kwargs")
        if not isinstance(chat_kwargs, dict):
            chat_kwargs = {}
        chat_kwargs["enable_thinking"] = False
        body_json["chat_template_kwargs"] = chat_kwargs
    elif current_max_tokens > MAX_MAX_TOKENS:
        body_json["max_tokens"] = MAX_MAX_TOKENS

    # n>1 multiplica o custo de GPU por resposta — sem valor pro caso de uso
    # BYOE (ferramentas de código pedem 1 completion) e sem teto era um vetor
    # de abuso trivial (n=100 = 100x o custo de uma request só)
    if isinstance(body_json.get("n"), int):
        body_json["n"] = 1

    # Default de sampling da stack (migration 0035) — só entra quando o
    # cliente NÃO mandou o parâmetro; 0.0 é um valor explícito válido, por
    # isso o check é "not in body_json", não um isinstance/truthy check.
    apply_stack_sampling_defaults(body_json, entry)

    for param, lo, hi in (
        ("temperature", 0.0, 2.0),
        ("top_p", 0.0, 1.0),
        ("frequency_penalty", -2.0, 2.0),
        ("presence_penalty", -2.0, 2.0),
    ):
        value = body_json.get(param)
        if isinstance(value, (int, float)):
            body_json[param] = min(max(value, lo), hi)
    body_json.pop("logit_bias", None)
    # cache_salt é campo DO SERVIDOR: quem decide o valor é o agent dentro do
    # pod (docker/agent/proxy_policy.py), a partir do stack_id. Deixar o
    # cliente escolher seria deixá-lo colidir de propósito com o cache de
    # outro tenant. A defesa real é o pop incondicional do agent — o pod é
    # alcançável direto pela URL pública, então tirar aqui não fecha nada
    # sozinho. Isto é documentação executável, não a trava.
    body_json.pop("cache_salt", None)

    messages = body_json.get("messages")
    if not isinstance(messages, list):
        # /v1/completions (prompt cru, sem messages): mesmo orçamento de
        # janela do chat — embeddings e afins não têm max_tokens e o
        # apply_context_budget vira no-op de clamp neles
        if isinstance(body_json.get("prompt"), str):
            prompt_text = body_json["prompt"]
            heuristic_est = estimate_prompt_tokens(extra_texts=[prompt_text])
            est_tokens, kind = await resolve_est_tokens(machine, heuristic_est, prompt_text)
            budget = apply_context_budget(body_json, machine, est_tokens=est_tokens, kind=kind)
            if budget_out is not None:
                budget_out["budget"] = budget
        return body_json

    if len(messages) > MAX_MESSAGES:
        raise HTTPException(status_code=400, detail="número de mensagens excede o limite")

    # roles fora da whitelist são descartadas silenciosamente — nenhuma
    # ferramenta BYOE legítima deveria mandar algo além disso, e um role
    # desconhecido não tem tratamento definido no chat template do vLLM
    messages = [m for m in messages if m.get("role") in ALLOWED_ROLES]

    # normaliza "system": no máximo UM, sempre no índice 0 — o chat template
    # do Qwen3.x rejeita ("System message must be at the beginning") qualquer
    # role "system" que não seja a primeira mensagem. Se o cliente já mandou
    # um (comum em ferramentas BYOE — Cursor/Codex/Claude Code embutem o
    # próprio system prompt pra tool-calling/formatação), respeita o dele e
    # NÃO injeta o da stack — evita duas inserções (uma em 0, outra antes do
    # último user) que quebravam a chamada inteira, e preserva o
    # funcionamento normal da ferramenta cliente. Sem system do cliente,
    # injeta o system_prompt da stack + contexto do RAG.
    client_systems = [m for m in messages if m.get("role") == "system"]
    messages = [m for m in messages if m.get("role") != "system"]

    # text_of e não isinstance(str): `content` em lista de partes tipadas é
    # protocolo OpenAI válido, e antes era descartado — o system do cliente
    # sumia E o fallback abaixo não rodava (o `if` já tinha sido tomado),
    # deixando a request sem instrução nenhuma.
    client_system_text = "\n\n---\n\n".join(
        filter(None, (text_of(c.get("content")) for c in client_systems))
    )

    # A condição é o TEXTO, não a presença da mensagem: um system vazio não
    # carrega instrução, então não deve suprimir o prompt da stack. Antes,
    # `{"role":"system","content":""}` engolia a configuração da conta e não
    # colocava nada no lugar.
    if client_system_text:
        messages.insert(0, {"role": "system", "content": client_system_text})
    else:
        system_message = await build_stack_system_message(messages, entry)
        if system_message:
            messages.insert(0, system_message)

    # Recorta imagem acima do que o pod aceita (machines.max_images_per_prompt,
    # lido do --limit-mm-per-prompt do template). Tem que ser ANTES da
    # estimativa de tokens, senão o orçamento conta imagem que não vai ser
    # enviada. Sem isso o vLLM devolveria 400, e num cliente que reenvia a
    # conversa toda esse 400 se repete pra sempre — ver content_policy.py.
    messages, dropped_images = clamp_media(
        messages, machine.get("max_images_per_prompt")
    )
    if dropped_images:
        logger.info(
            "conteúdo: %d imagem(ns) recortada(s) da stack %s (teto do pod: %s)",
            dropped_images, stack_id, machine.get("max_images_per_prompt"),
        )
    body_json["messages"] = messages

    # Clamp dinâmico pela janela real do modelo — por último, com o prompt
    # FINAL (system da stack + RAG já injetados). Pode reduzir max_tokens
    # abaixo de MIN_MAX_TOKENS: entre truncar thinking e devolver o 400 cru
    # do vLLM, truncar é a degradação aceitável (o filtro de <think> tem
    # fallback pra stream cortado por length).
    tools = body_json.get("tools")
    heuristic_est = estimate_prompt_tokens(messages=messages, tools=tools)
    exact_text = prompt_text_for_tokenize(messages=messages, tools=tools)
    # image_tokens: exact_text não leva as imagens (base64 não tokeniza como
    # texto), então o custo delas tem que voltar por fora na contagem exata —
    # senão perto do limite um prompt com imagem vale menos do que valia pela
    # heurística. Único call site com messages, logo o único que precisa disso.
    image_tokens = count_images(messages) * CONTEXT_IMAGE_TOKENS
    est_tokens, kind = await resolve_est_tokens(
        machine, heuristic_est, exact_text, image_tokens=image_tokens
    )
    budget = apply_context_budget(body_json, machine, est_tokens=est_tokens, kind=kind)
    if budget_out is not None:
        budget_out["budget"] = budget
    return body_json


async def build_stack_system_message(messages: list, entry: dict) -> dict | None:
    """system_prompt configurado (da CHAVE ou da STACK) + contexto de RAG
    (top-k da base de conhecimento da STACK), pra quando o cliente não mandou
    system próprio.

    Reaproveita resolve_key_stack (mesmo helper do roteamento de máquina,
    commit 7e64aa4) para saber qual stack da conta está servindo o request —
    sem isso, contas com múltiplas stacks vazavam o mesmo prompt/RAG entre
    todas elas (system_prompt/knowledge_chunks eram só por account_id)."""
    stack, _ = resolve_key_stack(entry)

    system_parts = []
    # A instrução é da stack (migration 0020) a não ser que a chave tenha a
    # própria (migration 0053) — ver key_prompt.py para a precedência. O RAG
    # abaixo NÃO acompanha essa escolha: a base de conhecimento é da stack e
    # continua valendo, porque a chave troca o "como responder", não o "sobre
    # o quê".
    system_prompt = resolve_system_prompt(entry, stack)
    if system_prompt:
        system_parts.append(system_prompt)

    last_user = next(
        (m for m in reversed(messages) if m.get("role") == "user"), None
    )
    if last_user and isinstance(last_user.get("content"), str):
        embedding = await embed_query(last_user["content"])
        if embedding:
            chunks = await supa.match_knowledge_chunks(
                entry["account_id"],
                stack["id"] if stack else None,
                embedding,
                RAG_TOP_K,
            )
            if chunks:
                system_parts.append(
                    "Contexto relevante da base de conhecimento:\n"
                    + "\n---\n".join(chunks)
                )

    if not system_parts:
        return None
    return {"role": "system", "content": "\n\n---\n\n".join(system_parts)}


async def build_stack_instructions(entry: dict, last_user_text: str | None) -> str | None:
    """Equivalente a build_stack_system_message, mas devolve só o texto: a
    Responses API (Codex) usa o campo "instructions", não uma mensagem
    role=system dentro de "input"."""
    stack, _ = resolve_key_stack(entry)

    system_parts = []
    system_prompt = resolve_system_prompt(entry, stack)
    if system_prompt:
        system_parts.append(system_prompt)

    if last_user_text:
        embedding = await embed_query(last_user_text)
        if embedding:
            chunks = await supa.match_knowledge_chunks(
                entry["account_id"],
                stack["id"] if stack else None,
                embedding,
                RAG_TOP_K,
            )
            if chunks:
                system_parts.append(
                    "Contexto relevante da base de conhecimento:\n"
                    + "\n---\n".join(chunks)
                )

    if not system_parts:
        return None
    return "\n\n---\n\n".join(system_parts)


def _last_user_text_from_responses_input(input_items) -> str | None:
    if not isinstance(input_items, list):
        return None
    for item in reversed(input_items):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [
                part.get("text") for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            ]
            if texts:
                return "\n".join(texts)
    return None


async def validate_responses_body(
    body_json: dict, entry: dict, rewrite_model: bool, machine: dict, stack_id: str
) -> dict:
    """Equivalente a validate_body pro formato da Responses API (Codex CLI):
    "input" no lugar de "messages", "instructions" no lugar de um system
    message, "max_output_tokens" no lugar de "max_tokens"."""
    pin_model(body_json, stack_id, rewrite_model, machine)

    # nunca persistir a resposta recuperável por outro tenant via
    # GET /v1/responses/{id} — esse subpath nem está na allowlist, mas
    # store=false garante que não sobra nada pra recuperar de qualquer jeito
    body_json["store"] = False

    max_output_tokens = body_json.get("max_output_tokens")
    if not isinstance(max_output_tokens, int) or max_output_tokens < MIN_MAX_TOKENS:
        body_json["max_output_tokens"] = MIN_MAX_TOKENS
    elif max_output_tokens > MAX_MAX_TOKENS:
        body_json["max_output_tokens"] = MAX_MAX_TOKENS

    apply_stack_sampling_defaults(body_json, entry)

    for param, lo, hi in (("temperature", 0.0, 2.0), ("top_p", 0.0, 1.0)):
        value = body_json.get(param)
        if isinstance(value, (int, float)):
            body_json[param] = min(max(value, lo), hi)

    # campo do servidor — mesmo motivo do validate_body acima
    body_json.pop("cache_salt", None)

    input_items = body_json.get("input")
    if isinstance(input_items, list):
        if len(input_items) > MAX_MESSAGES:
            raise HTTPException(status_code=400, detail="número de itens excede o limite")
        # bug conhecido do Codex (openai/codex#12669): ao reenviar itens de
        # turnos anteriores (mensagens do assistente, chamadas de
        # ferramenta), às vezes vêm sem "id"/"status" — a validação estrita
        # do vLLM rejeita com 502. Sintetiza os dois só nesses itens (nunca
        # em input NOVO do usuário, que legitimamente não tem esses campos)
        for i, item in enumerate(input_items):
            if not isinstance(item, dict):
                continue
            is_replayed_output = item.get("role") == "assistant" or item.get("type") in (
                "function_call",
                "function_call_output",
            )
            if is_replayed_output:
                item.setdefault("id", f"synth_{i}")
                item.setdefault("status", "completed")

        # mesmo recorte de imagem do validate_body, na forma da Responses API
        # ("input_image"/"input_text" em vez de "image_url"/"text") — sem isso
        # uma imagem acima do teto do pod viraria 400 e, como o Codex reenvia o
        # input inteiro a cada turno, o 400 se repetiria pra sempre
        input_items, dropped_images = clamp_media(
            input_items,
            machine.get("max_images_per_prompt"),
            text_part_type="input_text",
        )
        body_json["input"] = input_items
        if dropped_images:
            logger.info(
                "conteúdo: %d imagem(ns) recortada(s) da stack %s (teto do pod: %s)",
                dropped_images, stack_id, machine.get("max_images_per_prompt"),
            )

    if not body_json.get("instructions"):
        instructions = await build_stack_instructions(
            entry, _last_user_text_from_responses_input(input_items)
        )
        if instructions:
            body_json["instructions"] = instructions

    # mesmo clamp dinâmico do validate_body, no campo da Responses API; o
    # "input" pode ser string ou lista de itens — json.dumps cobre os dois.
    # ensure_ascii=False: mesmo motivo do estimate_prompt_tokens — o escape
    # \uXXXX inflaria texto acentuado ~2x
    tools = body_json.get("tools")
    extra_texts = [
        json.dumps(body_json.get("input") or "", ensure_ascii=False),
        body_json.get("instructions") or "",
    ]
    heuristic_est = estimate_prompt_tokens(tools=tools, extra_texts=extra_texts)
    exact_text = prompt_text_for_tokenize(tools=tools, extra_texts=extra_texts)
    est_tokens, kind = await resolve_est_tokens(machine, heuristic_est, exact_text)
    apply_context_budget(
        body_json, machine, field="max_output_tokens", est_tokens=est_tokens, kind=kind
    )
    return body_json


THINK_CLOSE = "</think>"

# planos cujo modelo padrão roda com "thinking" ligado — sem
# ENABLE_REASONING_PARSER=true no template (ver docker/entrypoint.sh), o vLLM
# sobe sem --reasoning-parser e o raciocínio inteiro vaza pro campo "content"
# que o cliente exibe. Filtrado aqui porque o produto é BYOE: nenhuma
# ferramenta cliente (Cursor, Continue etc.) sabe separar isso sozinha.
# Quando o template liga ENABLE_REASONING_PARSER, o vLLM já separa o
# raciocínio em "reasoning_content" e este filtro se desliga sozinho pra
# essa resposta (ver o branch "reasoning_content" em
# filtered_reasoning_stream) — streaming deixa de ficar represado.
# Pro (Qwen3.6-27B) validado em 17/07/2026: 14/15 respostas fecham com
# </think> (a exceção foi truncada por length — coberta pelo fallback do
# filtro, que devolve o buffer acumulado no fim do stream).
# "VibeCoder" é o nome antigo de "Go": fica aceito até a migration 0049 rodar
# em produção, senão o plano sai do set na janela entre deploy e migration e o
# filtro de <think> desliga sozinho. Remover depois.
REASONING_LEAK_PLANS = {"Go", "VibeCoder", "Pro"}

# planos cujo pod é COMPARTILHADO entre várias stacks/tenants (ver
# check_concurrency) — hoje coincide em membros com REASONING_LEAK_PLANS, mas
# são eixos diferentes (parser de reasoning vs. topologia do pod) que podem
# divergir; não reaproveitar um pelo outro.
SHARED_POD_PLANS = {"Go", "VibeCoder", "Pro"}


def split_reasoning(text: str) -> tuple[str | None, str]:
    """Separa o bloco de raciocínio da resposta final. Modelos com thinking
    ligado não emitem a tag de abertura no texto gerado (o chat template já
    injeta "<think>\\n" no prompt) — só a de fechamento. Sem </think> na
    string, devolve (None, texto original): não há nada pra filtrar."""
    idx = text.find(THINK_CLOSE)
    if idx == -1:
        return None, text
    return text[:idx], text[idx + len(THINK_CLOSE) :].lstrip("\n")


async def filtered_reasoning_stream(upstream: httpx.Response, flight_key: tuple, log_ctx: dict):
    """Envolve o stream SSE bruto do vLLM suprimindo os chunks de raciocínio
    (antes de </think>) e só repassando ao cliente o que vem depois. Se o
    teto de tokens for atingido sem nunca fechar </think> (raro, ~0-5% mesmo
    com o piso de max_tokens), devolve o raciocínio acumulado no chunk final
    em vez de descartar a resposta em silêncio.

    `log_ctx` alimenta o registro fire-and-forget em gateway_requests
    (migration 0038) no finally — `usage` é capturado de graça aqui porque
    todo chunk já passa por json.loads pra filtrar o raciocínio. O status
    gravado é o do UPSTREAM no caso normal, mas vira 504 quando o watchdog
    aborta: sem isso um stream morto por timeout ficava registrado como 200 e
    era invisível pra qualquer investigação depois."""
    buffer_text = ""
    in_reasoning = True
    pending = b""
    usage = None
    status_code = upstream.status_code
    timed_out = None
    try:
        try:
            async for raw in aiter_bytes_watchdog(
                upstream,
                ttft_s=CHAT_STREAM_TTFT_TIMEOUT_S,
                idle_s=CHAT_STREAM_IDLE_TIMEOUT_S,
                log_label=str(flight_key),
            ):
                pending += raw
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    stripped = line.strip()

                    # o chunk final de usage (choices: [], só "usage") chega
                    # DEPOIS que in_reasoning já virou False (raciocínio já
                    # fechado) — sem isso aqui, o branch abaixo repassa a
                    # linha crua e sai do loop antes de nunca ver esse chunk,
                    # deixando tokens_in/tokens_out null pra sempre nos
                    # planos com filtro de raciocínio (Go/Pro).
                    if usage is None and b'"usage"' in stripped and stripped.startswith(b"data:"):
                        usage_payload = stripped[len(b"data:") :].strip()
                        if usage_payload not in (b"[DONE]", b""):
                            try:
                                maybe_chunk = json.loads(usage_payload)
                            except Exception:
                                maybe_chunk = None
                            if isinstance(maybe_chunk, dict) and maybe_chunk.get("usage"):
                                usage = maybe_chunk["usage"]

                    if not in_reasoning or not stripped.startswith(b"data:") or stripped in (
                        b"data: [DONE]",
                        b"data:[DONE]",
                    ):
                        yield line + b"\n"
                        continue

                    payload = stripped[len(b"data:") :].strip()
                    try:
                        chunk = json.loads(payload)
                    except Exception:
                        yield line + b"\n"
                        continue

                    if isinstance(chunk, dict) and chunk.get("usage"):
                        usage = chunk["usage"]

                    choices = chunk.get("choices") or []
                    choice0 = choices[0] if choices and isinstance(choices[0], dict) else None
                    if choice0 is None:
                        yield line + b"\n"
                        continue

                    delta = choice0.get("delta") or {}

                    # vLLM com --reasoning-parser (ENABLE_REASONING_PARSER, ver
                    # entrypoint.sh) já separa o raciocínio em
                    # "reasoning_content" — nesse caso "content" nunca vem
                    # com <think>, e o buffer abaixo nunca veria um </think>
                    # pra fechar, represando a resposta INTEIRA até o fim do
                    # stream (todo o texto sairia de uma vez só no fallback,
                    # quebrando streaming incremental). Detectar isso aqui e
                    # desligar o filtro nesta resposta evita esse represamento.
                    if "reasoning_content" in delta:
                        in_reasoning = False
                        if buffer_text:
                            flushed_delta = dict(delta)
                            flushed_delta["content"] = buffer_text
                            flushed_choice = dict(choice0)
                            flushed_choice["delta"] = flushed_delta
                            flushed_chunk = dict(chunk)
                            flushed_chunk["choices"] = [flushed_choice]
                            yield b"data: " + json.dumps(flushed_chunk).encode() + b"\n"
                            buffer_text = ""
                        yield line + b"\n"
                        continue

                    content = delta.get("content")
                    finish_reason = choice0.get("finish_reason")
                    if content:
                        buffer_text += content

                    if THINK_CLOSE in buffer_text:
                        _, visible = split_reasoning(buffer_text)
                        in_reasoning = False
                        buffer_text = ""
                        if visible or finish_reason:
                            delta = dict(delta)
                            delta["content"] = visible
                            delta.setdefault("role", "assistant")
                            choice0["delta"] = delta
                            yield b"data: " + json.dumps(chunk).encode() + b"\n"
                        continue

                    if finish_reason:
                        # bateu finish_reason sem nunca ver </think> — devolve o
                        # que foi acumulado em vez de sumir com a resposta inteira
                        delta = dict(delta)
                        delta["content"] = buffer_text
                        delta.setdefault("role", "assistant")
                        choice0["delta"] = delta
                        buffer_text = ""
                        in_reasoning = False
                        yield b"data: " + json.dumps(chunk).encode() + b"\n"
                        continue

                    continue  # ainda dentro do raciocínio, sem finish_reason -> suprime
            if pending:
                yield pending
        except UpstreamStreamTimeout as e:
            # o upstream estourou o teto sem mandar byte. NÃO é fim de stream:
            # o cliente precisa saber que falhou, senão recebe um [DONE] limpo
            # e trata como resposta completa — foi assim que 18 de 36 requests
            # de um load test "passaram" com 200 tendo entregado nada.
            timed_out = e
            status_code = 504
        except (httpx.HTTPError, ConnectionError, OSError):
            # a conexão com o upstream (agent/vLLM) morreu no meio do stream —
            # visto sob concorrência pesada (conexão do pool resetada pelo
            # Cloudflare enquanto ociosa). Não deixa a exceção estourar em
            # silêncio: cai no fallback abaixo, que devolve o que já foi
            # acumulado em vez de fechar a resposta sem nada.
            status_code = 502
        fechou_stream = False
        if in_reasoning and buffer_text:
            # a conexão upstream acabou sem nunca fechar </think> nem mandar um
            # finish_reason (visto sob concorrência pesada — provável preempção/
            # aborto do vLLM, não um bug de framing) — melhor devolver o que foi
            # acumulado do que deixar o cliente sem nenhuma resposta
            fallback = {
                "object": "chat.completion.chunk",
                "choices": [
                    {"index": 0, "delta": {"role": "assistant", "content": buffer_text}, "finish_reason": "stop"}
                ],
            }
            yield b"data: " + json.dumps(fallback).encode() + b"\n\n"
            fechou_stream = True
        if timed_out is not None:
            # erro no corpo do SSE porque o 200 já foi pro cliente junto com o
            # cabeçalho, muito antes de o corpo morrer — não dá mais pra mudar
            # o status HTTP. O formato é o de erro da OpenAI, que é o que os
            # clientes desse endpoint sabem ler.
            yield b"data: " + json.dumps({
                "error": {
                    "message": (
                        "a máquina não entregou resposta a tempo "
                        f"({timed_out.phase}, {timed_out.waited:.0f}s) — "
                        "tente novamente ou reduza o tamanho do prompt"
                    ),
                    "type": "upstream_timeout",
                    "code": "upstream_timeout",
                },
            }).encode() + b"\n\n"
            fechou_stream = True
        if fechou_stream:
            # só quando FOMOS nós a fechar. No caminho normal o [DONE] vem do
            # próprio upstream e já foi repassado — emitir outro duplicaria.
            yield b"data: [DONE]\n\n"
    finally:
        await upstream.aclose()
        release_flight(flight_key)
        log_gateway_request(**log_ctx, status_code=status_code, stream=True, usage=usage)


# ---------- Claude Code (Anthropic Messages API) ----------
#
# Registrados ANTES do catch-all /v1/{path:path} abaixo: o Starlette casa
# rotas na ordem de declaração, e o catch-all engoliria /v1/messages* se
# viesse primeiro. O Claude Code só fala esse formato (não tem suporte a
# backend OpenAI-compatível) — ver anthropic_compat.py pro porquê e os
# limites da tradução.


@app.post("/v1/messages")
async def anthropic_messages(
    request: Request,
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None),
):
    started = time.monotonic()
    raw_body = await request.body()
    if len(raw_body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="corpo da requisição excede o limite")

    entry, key_hash, bearer_header = await authenticate_anthropic(
        authorization, x_api_key, request.headers, "messages"
    )
    account_id = entry["account_id"]
    _, key_plan = resolve_key_stack(entry)
    check_rate_limit(key_hash, key_plan)
    await check_token_quota(account_id, key_plan, entry.get("purpose", "customer"))

    machine, rewrite_model, effective_plan, stack_id = await resolve_route(account_id, entry)
    await maybe_touch(stack_id, machine["id"])

    flight_key = (stack_id, machine["id"])
    in_flight[flight_key] += 1
    check_concurrency(flight_key, machine, effective_plan)

    log_ctx = dict(
        account_id=account_id, stack_id=stack_id, api_key_id=entry["api_key_id"],
        machine_id=machine["id"], path="messages",
        model=effective_model_name(stack_id, rewrite_model, machine),
        user_agent=request.headers.get("user-agent"), started=started,
    )

    try:
        anthropic_body = json.loads(raw_body)
    except Exception:
        release_flight(flight_key)
        raise HTTPException(status_code=400, detail="corpo inválido")

    openai_body, requested_model = anthropic_to_openai_request(anthropic_body)
    is_stream = bool(anthropic_body.get("stream"))
    # mesmo filtro de <think> do chat/completions (main.py:REASONING_LEAK_PLANS)
    # — sem isso, Claude Code apontado pra um plano sem reasoning-parser
    # (ver ENABLE_REASONING_PARSER) recebia o raciocínio cru misturado no texto
    filter_reasoning = effective_plan in REASONING_LEAK_PLANS

    budget_out: dict = {}
    try:
        # mesmo validate_body do chat/completions: trava o model, aplica
        # piso/teto de max_tokens e clamp de parâmetros. O "system"
        # convertido acima já entra como client_systems (é o system prompt
        # do próprio Claude Code) — respeitado, sem injetar o da stack por
        # cima (mesma política de todos os outros canais)
        openai_body = await validate_body(
            openai_body, entry, rewrite_model, machine, stack_id, budget_out=budget_out
        )
    except ContextWindowExceeded as exc:
        release_flight(flight_key)
        # O estouro de contexto é o evento mais importante deste endpoint (mata
        # a sessão do usuário) e até aqui não deixava rastro NENHUM: nem em
        # gateway_requests, nem no log de aplicação — só a linha de access log
        # do uvicorn. Foi o que tornou o incidente indiagnosticável.
        budget = budget_out.get("budget")
        logger.warning(
            "contexto estourado em messages: est=%s (%s) janela=%s ua=%s stack=%s",
            getattr(budget, "est_tokens", "?"),
            getattr(getattr(budget, "kind", None), "value", "?"),
            machine.get("max_model_len"), request.headers.get("user-agent"), stack_id,
        )
        log_gateway_request(**log_ctx, status_code=400, stream=is_stream, usage=None)
        # o shape vai na exceção: o cliente aqui é sempre Anthropic, e deixar o
        # handler adivinhar pela URL é frágil (ver context_window_exceeded_handler)
        exc.shape = "anthropic"
        raise
    except HTTPException:
        release_flight(flight_key)
        raise

    # tokens de input a reportar ao cliente. O Claude Code rastreia o contexto
    # pelo usage.input_tokens que a gente devolve; em streaming o vLLM só manda
    # usage no chunk FINAL, então sem esta estimativa o message_start sai com 0
    # e o contador do cliente nunca sai do lugar — auto-compact nunca dispara.
    # Na faixa que importa (perto do teto) este número é a contagem exata do
    # tokenizer, e o valor real do vLLM corrige no message_delta.
    budget = budget_out.get("budget")
    log_ctx["budget"] = budget
    input_tokens_estimate = budget.est_tokens if budget else 0

    upstream_body = json.dumps(openai_body).encode()

    # Streaming: o read do httpx aqui é só a rede de segurança EXTERNA — quem
    # controla o prazo é o watchdog de duas fases dentro do gerador
    # (anthropic_compat), que sabe distinguir prefill de silêncio no meio do
    # stream. A folga de 15s garante que seja sempre o watchdog a responder
    # primeiro, com mensagem útil, em vez do httpx com um ReadTimeout cru.
    # Não-streaming: sem chunks, o read tem que cobrir a geração inteira até
    # max_tokens — ver MESSAGES_NONSTREAM_TIMEOUT_S acima.
    # connect/write/pool continuam os do client (5s/10s/10s): só o read muda.
    upstream_timeout = httpx.Timeout(
        (MESSAGES_STREAM_TTFT_TIMEOUT_S + 15.0) if is_stream else MESSAGES_NONSTREAM_TIMEOUT_S,
        connect=5.0, write=10.0, pool=10.0,
    )

    try:
        upstream_req = proxy_client.build_request(
            "POST",
            f"{machine['public_url']}/v1/chat/completions",
            content=upstream_body,
            headers={"Authorization": bearer_header, "Content-Type": "application/json"},
            timeout=upstream_timeout,
        )
        upstream = await proxy_client.send(upstream_req, stream=True)
    except httpx.ReadTimeout as e:
        # conexão abriu normalmente, máquina pode estar saudável (ver
        # MESSAGES_NONSTREAM_TIMEOUT_S) — só não respondeu dentro da janela.
        # Mensagem separada da de "máquina indisponível" abaixo: aqui não dá
        # pra saber se ela ainda vai terminar, então "tente novamente" é
        # enganoso — o cliente deve saber que foi timeout, não indisponibilidade.
        release_flight(flight_key)
        logger.warning(
            "anthropic proxy: timeout aguardando resposta de %s (%s)", flight_key, e
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "a máquina não respondeu a tempo (geração longa ou timeout "
                "insuficiente) — considere usar stream:true para max_tokens altos"
            ),
        )
    except httpx.HTTPError as e:
        # falha de conexão de verdade (recusada, DNS, pod fora do ar etc.) —
        # aqui sim "indisponível" é a descrição correta.
        release_flight(flight_key)
        logger.warning("anthropic proxy: upstream indisponível para %s (%s)", flight_key, e)
        raise HTTPException(status_code=503, detail="máquina indisponível, tente novamente")
    except BaseException:
        release_flight(flight_key)
        raise

    if is_stream:
        if upstream.status_code >= 400:
            # o upstream (vLLM) recusou a request antes de gerar qualquer chunk
            # SSE — o corpo é um erro OpenAI/FastAPI comum (JSON, não SSE). Se
            # deixarmos isso cair no anthropic_sse_from_openai_stream, o loop
            # de parsing (que só entende linhas "data: ...") descarta o corpo
            # inteiro e emite um stream vazio "bem-sucedido" (content: [],
            # stop_reason: end_turn) com status_code=400 por cima — o Claude
            # Code mostra "API Error: 400" seguido do stream vazio, sem
            # nenhuma pista da causa real. Aqui devolvemos o erro de verdade,
            # no formato que a Anthropic Messages API usa.
            error_raw = await upstream.aread()
            await upstream.aclose()
            release_flight(flight_key)
            logger.warning(
                "anthropic proxy: upstream %s retornou %s para %s: %s",
                machine["id"], upstream.status_code, flight_key, error_raw[:500],
            )
            try:
                error_detail = json.loads(error_raw)
                message = (
                    error_detail.get("message")
                    or (error_detail.get("error") or {}).get("message")
                    or error_detail.get("detail")
                    or error_raw.decode(errors="replace")
                )
            except Exception:
                message = error_raw.decode(errors="replace") or "erro desconhecido do modelo"
            log_gateway_request(**log_ctx, status_code=upstream.status_code, stream=True, usage=None)
            return JSONResponse(
                status_code=upstream.status_code,
                content=anthropic_error_body(message),
            )

        def _on_stream_done(usage: dict | None) -> None:
            release_flight(flight_key)
            log_gateway_request(**log_ctx, status_code=upstream.status_code, stream=True, usage=usage)

        return StreamingResponse(
            anthropic_sse_from_openai_stream(
                upstream, requested_model,
                on_done=_on_stream_done,
                filter_reasoning=filter_reasoning,
                input_tokens_estimate=input_tokens_estimate,
                ttft_timeout_s=MESSAGES_STREAM_TTFT_TIMEOUT_S,
                idle_timeout_s=MESSAGES_STREAM_IDLE_TIMEOUT_S,
                ping_interval_s=ANTHROPIC_SSE_PING_INTERVAL_S,
                log_label=f"{stack_id}/{machine['id']}",
            ),
            status_code=upstream.status_code,
            media_type="text/event-stream",
            # SSE não deve ser bufferizado por proxy nenhum no caminho — sem
            # isso um intermediário pode segurar os chunks e devolver tudo no
            # fim, o que anula o streaming e o heartbeat de ping junto.
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    usage = None
    not_json = False
    try:
        raw = await upstream.aread()
        try:
            openai_resp = json.loads(raw)
            usage = openai_resp.get("usage")
            if filter_reasoning:
                for choice in openai_resp.get("choices", []):
                    message = choice.get("message")
                    if isinstance(message, dict) and isinstance(message.get("content"), str):
                        reasoning, visible = split_reasoning(message["content"])
                        if reasoning is not None:
                            message["content"] = visible
            anthropic_resp = openai_to_anthropic_response(
                openai_resp, requested_model,
                input_tokens_fallback=input_tokens_estimate,
            )
            raw = json.dumps(anthropic_resp).encode()
        except Exception:
            not_json = True  # resposta não é o JSON esperado
    finally:
        await upstream.aclose()
        release_flight(flight_key)
        log_gateway_request(**log_ctx, status_code=upstream.status_code, stream=False, usage=usage)

    if not_json and upstream.status_code >= 400:
        # upstream não devolveu JSON — ex.: página de erro HTML do Cloudflare
        # do RunPod, quando o timeout DELES estoura antes do vLLM terminar
        # (524 "A timeout occurred", visto ao vivo). Sem isso, o HTML cru ia
        # pro cliente com content-type mentindo "application/json", quebrando
        # o parser de qualquer SDK. Mesmo tratamento do branch streaming
        # acima (upstream.status_code >= 400): erro Anthropic-shaped.
        message = raw.decode(errors="replace").strip() or "erro desconhecido do modelo"
        if message.startswith("<"):
            # corpo é HTML (página de erro de proxy) — não é uma mensagem
            # útil pro cliente, troca por algo genérico em vez de vazar HTML
            message = "erro do servidor upstream (resposta não-JSON)"
        logger.warning(
            "anthropic proxy: upstream %s retornou %s não-JSON para %s",
            machine["id"], upstream.status_code, flight_key,
        )
        return JSONResponse(status_code=upstream.status_code, content=anthropic_error_body(message))

    return Response(content=raw, status_code=upstream.status_code, media_type="application/json")


@app.post("/v1/messages/count_tokens")
async def anthropic_count_tokens(
    request: Request,
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None),
):
    """Tokens de input do prompt, pela MESMA conta que a admissão usa.

    O cliente (Claude Code) chama isso pra decidir quando compactar, então
    divergir da admissão é o pior dos mundos: ele compacta cedo demais
    (desperdiça janela) ou tarde demais (leva 400). Por isso reusa
    resolve_est_tokens — heurística ~4 chars/token longe do teto, contagem real
    do tokenizer do vLLM perto dele, exatamente como em validate_body. Antes
    daqui saía só a heurística.

    Devolve o número CRU, sem margem de segurança: a margem é do orçamento do
    servidor (reserved_tokens_for): aplicá-la aqui faria o cliente compactar
    ~20% mais cedo do que precisa.

    Passa por rate limit (mas não por quota/concorrência): deixou de ser um
    cálculo puramente local — perto do teto faz uma chamada ao pod."""
    entry, key_hash, _bearer = await authenticate_anthropic(
        authorization, x_api_key, request.headers, "messages/count_tokens"
    )
    _, key_plan = resolve_key_stack(entry)
    check_rate_limit(key_hash, key_plan)
    raw_body = await request.body()
    if len(raw_body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="corpo da requisição excede o limite")
    try:
        anthropic_body = json.loads(raw_body)
    except Exception:
        raise HTTPException(status_code=400, detail="corpo inválido")
    openai_body, _ = anthropic_to_openai_request(anthropic_body)
    messages = openai_body.get("messages")
    tools = openai_body.get("tools")
    # inclui as tools: nos clientes agênticos são a maior fatia do prompt, e
    # sem elas o Claude Code subestimava o uso e compactava tarde demais
    heuristic_est = estimate_prompt_tokens(messages=messages, tools=tools)
    # sem máquina viva (stack pausada, pod dormindo) não há tokenizer pra
    # consultar — a heurística é a melhor resposta disponível, e é melhor que
    # acordar pod num endpoint de metadado
    machine = await resolve_machine_readonly(entry)
    if machine is None:
        # sem máquina o número sai da heurística PURA, sem nem o gate de
        # proximidade do teto — vale logar, porque é o caminho em que o cliente
        # é pior informado e até aqui este endpoint não deixava rastro nenhum
        logger.info(
            "count_tokens: %d tokens (heuristica, sem maquina viva) ua=%s",
            heuristic_est, request.headers.get("user-agent"),
        )
        return {"input_tokens": heuristic_est}
    est_tokens, kind = await resolve_est_tokens(
        machine,
        heuristic_est,
        prompt_text_for_tokenize(messages=messages, tools=tools),
        image_tokens=count_images(messages) * CONTEXT_IMAGE_TOKENS,
    )
    logger.info(
        "count_tokens: %d tokens (%s) janela=%s ua=%s",
        est_tokens, kind.value, machine.get("max_model_len"),
        request.headers.get("user-agent"),
    )
    return {"input_tokens": est_tokens}


# paths do vLLM que o gateway repassa — qualquer coisa fora daqui (ex.:
# load_lora_adapter, unload_lora_adapter, tokenize) nunca chega nem a
# autenticar. Antes desta allowlist, um usuário comum autenticado tinha
# acesso irrestrito a QUALQUER endpoint /v1/* do vLLM, incluindo os
# administrativos (ver furo B do plano — cross-tenant via load/unload de
# adapter alheio). "models" é permitido mas tratado à parte (filtra acct-*).
ALLOWED_V1: dict[str, set[str]] = {
    "chat/completions": {"POST"},
    "completions": {"POST"},
    "embeddings": {"POST"},
    "models": {"GET"},
    "responses": {"POST"},  # Codex CLI (0.59+) só fala essa API, não chat/completions
}


def release_flight(flight_key: tuple[str, str]) -> None:
    in_flight[flight_key] -= 1


MAX_USER_AGENT_CHARS = 200  # string do cliente: registrar sim, confiar não


def log_gateway_request(
    *, account_id: str, stack_id: str | None, api_key_id: str, machine_id: str,
    path: str, model: str | None, status_code: int, stream: bool,
    started: float, usage: dict | None = None, user_agent: str | None = None,
    budget: PromptBudget | None = None,
) -> None:
    """Log fire-and-forget de uma requisição completada (migration 0038,
    tabela gateway_requests). `started` é o time.monotonic() capturado na
    entrada do handler; `usage` é o dict OpenAI ({prompt_tokens,
    completion_tokens, ...}) quando disponível, ou None. Chamado só nos
    pontos que já têm flight_key (mesmo escopo de in_flight/release_flight)
    — nunca no caminho crítico da resposta ao cliente, e nunca deixa
    exceção escapar (spawn_tracked evita o bug conhecido de create_task sem
    referência sendo coletado pelo GC antes de terminar).

    `budget` (opcional) só alimenta a linha de log abaixo, não a tabela: é
    instrumentação temporária pra medir o erro de CONTEXT_EXACT_SAFETY_FACTOR
    (1.02) contra o prompt_tokens real do vLLM antes de mexer nele de novo —
    não vale uma migration."""
    # normaliza no ponto que grava, não em cada chamador: `usage` chega no
    # formato do protocolo que atendeu a requisição (chat ou Responses) e os
    # dois nomeiam as contagens de forma diferente — ver usage_norm.py. É
    # idempotente, então quem já manda o formato chat não é afetado.
    usage = normalize_usage(usage) or {}
    _log_estimate_drift(budget, usage, path)
    row = {
        "account_id": account_id,
        "stack_id": stack_id,
        "api_key_id": api_key_id,
        "machine_id": machine_id,
        "path": path,
        "model": model,
        # cru, classificado só na UI (lib/request-origin.ts) — ver o comment
        # da coluna na migration 0041. Truncado porque o header é do cliente.
        "user_agent": user_agent[:MAX_USER_AGENT_CHARS] if user_agent else None,
        "status_code": status_code,
        "stream": stream,
        "tokens_in": usage.get("prompt_tokens"),
        "tokens_out": usage.get("completion_tokens"),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    spawn_tracked(_write_gateway_request(row))


def _log_estimate_drift(budget: PromptBudget | None, usage: dict, path: str) -> None:
    """Compara o que o orçamento reservou com o que o vLLM realmente contou.

    A margem da contagem exata é de 2% (CONTEXT_EXACT_SAFETY_FACTOR) + 200
    tokens de chat template, apostando que o json.dumps que mandamos pro
    /tokenize sobra em relação ao template renderizado. Se a aposta estiver
    errada em algum template, o sintoma é o 400 cru do vLLM voltando — e num
    cliente que reenvia a conversa toda isso envenena a sessão inteira. Esta
    linha é o que permite ver a deriva ANTES do sintoma."""
    real = usage.get("prompt_tokens")
    if budget is None or not isinstance(real, int) or real <= 0:
        return
    drift = (budget.reserved - real) / real
    log = logger.warning if budget.reserved < real else logger.info
    log(
        "orçamento de contexto (%s): est=%d (%s) reservado=%d real=%d folga=%+.1f%% janela=%s",
        path, budget.est_tokens, budget.kind.value, budget.reserved, real,
        drift * 100, budget.max_model_len,
    )


async def _write_gateway_request(row: dict) -> None:
    try:
        await supa.insert_gateway_request(row)
    except httpx.HTTPError as e:
        logger.warning("gateway_requests: falha ao gravar log (%s)", e)


def machine_capacity(machine: dict) -> int:
    """Teto de sequências concorrentes do pod (espelha o --max-num-seqs real
    do deploy, machines.max_concurrent_seqs — migration 0028). Sem valor
    configurado ainda, cai no fallback global conservador em vez de travar
    o request."""
    cap = machine.get("max_concurrent_seqs")
    return cap if isinstance(cap, int) and cap > 0 else DEFAULT_MAX_CONCURRENT_SEQS


def check_concurrency(flight_key: tuple[str, str], machine: dict, plan: str) -> None:
    """Concorrência elástica por MÁQUINA, não por chave: uma stack sozinha no
    pod pode ocupar quase toda a capacidade; outras dividem o mesmo teto
    conforme aparecem. Em pod compartilhado (SHARED_POD_PLANS) reserva um
    piso mínimo (MIN_RESERVED_SLOTS_SHARED_POD) pra quem chegar depois nunca
    ficar 100% bloqueado esperando um tenant pesado — em pod dedicado não há
    vizinho pra proteger, o teto é a capacidade cheia.

    Chamada logo após incrementar in_flight[flight_key] (mesmo padrão do
    antigo teto por chave): se estourar, desfaz o incremento e rejeita."""
    machine_id = flight_key[1]
    reserved = MIN_RESERVED_SLOTS_SHARED_POD if plan in SHARED_POD_PLANS else 0
    ceiling = max(machine_capacity(machine) - reserved, 1)
    total_on_machine = sum(n for (_, m), n in in_flight.items() if m == machine_id)
    if total_on_machine > ceiling:
        release_flight(flight_key)
        raise HTTPException(
            status_code=429,
            detail="máquina no limite de capacidade concorrente no momento, tente novamente",
            headers={"Retry-After": "2"},
        )


# ---------- Extração estruturada de documento/imagem (PDF ou JPEG/PNG/WEBP → JSON) ----------
#
# ATENÇÃO À ORDEM: estas rotas têm que ser registradas ANTES do catch-all
# /v1/{path:path} logo abaixo. O Starlette casa rotas na ordem de registro, e
# o catch-all engoliria "documents/extract"/"images/extract" — que não estão
# em ALLOWED_V1 e virariam um 404 sem explicação nenhuma.
DOCUMENT_PATH = "documents/extract"
IMAGE_PATH = "images/extract"
# max_tokens da geração. Nada a ver com o piso MIN_MAX_TOKENS do chat: ali o
# piso existe porque um modelo com thinking gasta milhares de tokens antes da
# resposta; aqui o thinking é desligado (chat_template_kwargs abaixo) e a saída
# é só o JSON do schema. Teto configurável pra schema grande (documento com
# muitos itens), mas o default cobre com folga um formulário/nota típico.
DOCUMENT_MAX_TOKENS = int(os.environ.get("DOCUMENT_MAX_TOKENS", "4000"))
DOCUMENT_MAX_TOKENS_CEILING = int(os.environ.get("DOCUMENT_MAX_TOKENS_CEILING", "16000"))
# teto do schema. Um schema é uma estrutura de metadados, não dado: 64 KB já
# cobre documento com muitos campos aninhados com folga. O teto existe porque
# tanto o check_schema daqui quanto a compilação da gramática no vLLM têm custo
# proporcional ao tamanho — schema de megabytes é DoS, não caso de uso.
MAX_SCHEMA_BYTES = int(os.environ.get("MAX_SCHEMA_BYTES", "65536"))


def validate_extraction_output(content: str, schema: dict) -> dict:
    """Parse + validação do JSON gerado, contra o schema do CLIENTE.

    BLOQUEANTE de propósito, pra ser chamada em asyncio.to_thread — e essa é a
    parte importante: `schema` é input não confiável, e jsonschema compila
    `pattern` com o `re` do Python, que faz backtracking. Um schema com
    quantificador aninhado (o clássico "(a+)+$") contra uma string que NÃO casa
    tem custo exponencial no comprimento — medido aqui: 0,04s com 20 caracteres,
    0,16s com 22, ~12 horas com 40.

    Inline no event loop isso não seria lentidão de uma requisição, seria o
    gateway inteiro parado pra todos os tenants. Na thread, o dano fica contido
    na requisição que o causou (o rate limit por chave e o teto de concorrência
    limitam quantas dessas existem ao mesmo tempo).

    Risco residual assumido: a thread continua queimando um núcleo até
    terminar, porque Python não mata thread. O que se compra aqui é o event
    loop livre — que é a diferença entre "um cliente se prejudicou" e "o
    serviço caiu"."""
    data = json.loads(content)
    jsonschema.validate(data, schema)
    return data


async def _authenticate_for_extraction(
    authorization: str | None, schema: str, headers, path: str
) -> tuple[dict, str, str, dict]:
    """Auth + validação de schema — a parte IDÊNTICA entre /v1/documents/extract
    e /v1/images/extract. Devolve (entry, key_hash, account_id, parsed_schema).

    Autenticar ANTES de validar o schema, mesmo que o schema seja mais barato
    de checar: validar schema é trabalho de CPU (check_schema percorre o
    metaschema, e schema recursivo pode ir a RecursionError), e nada disso
    deve estar disponível a quem não tem chave. O authenticate é barato
    (key_cache em memória) e o rate limit passa a valer pra este caminho.

    `path` só alimenta a política de CLI (cli_policy.py). Extração nunca é rota
    de CLI, então nada é bloqueado aqui — passa pra que a lista de call-sites de
    authenticate seja uniforme e um endpoint novo não nasça sem o parâmetro."""
    entry, key_hash = await authenticate(authorization, headers, path)
    account_id = entry["account_id"]
    _, key_plan = resolve_key_stack(entry)
    check_rate_limit(key_hash, key_plan)
    await check_token_quota(account_id, key_plan, entry.get("purpose", "customer"))

    if len(schema) > MAX_SCHEMA_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"schema excede o limite de {MAX_SCHEMA_BYTES // 1024} KB",
        )
    try:
        parsed_schema = json.loads(schema)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"schema não é um JSON válido: {e}")
    if not isinstance(parsed_schema, dict):
        raise HTTPException(status_code=400, detail="schema precisa ser um objeto JSON")
    # Schema inválido rejeitado AQUI, antes de gastar OCR e uma inferência: sem
    # isso o erro só apareceria no validate da resposta, e o cliente receberia
    # um 502 ("o modelo não devolveu JSON aderente") por um problema que é do
    # schema dele. Except largo porque a família de erros não é só SchemaError:
    # um "$ref" remoto levanta _WrappedReferencingError (Unresolvable) e um
    # schema recursivo levanta RecursionError — nenhum dos dois herda de
    # SchemaError/ValidationError, e escapariam como 500 opaco.
    try:
        jsonschema.Draft202012Validator.check_schema(parsed_schema)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"schema inválido: {str(e)[:300]}")

    return entry, key_hash, account_id, parsed_schema


async def _run_extraction_pipeline(
    *,
    text: str,
    pages: int,
    ocr_used: bool,
    parsed_schema: dict,
    user: str | None,
    system: str | None,
    entry: dict,
    stack_id: str,
    machine: dict,
    rewrite_model: str | None,
    max_tokens: int | None,
    authorization: str | None,
    flight_key: tuple[str, str],
    log_ctx: dict,
    path_label: str,
) -> dict:
    """Monta o prompt, chama o pod e valida a saída — a parte IDÊNTICA entre
    /v1/documents/extract e /v1/images/extract, a partir do momento em que já
    se tem (texto, páginas, ocr_used) em mãos. O que muda entre as duas rotas
    é só COMO chegar até esses três valores (parsing do upload + extração)."""
    # `user` COMPÕE com a instrução padrão (ver build_messages): o cliente
    # acrescenta contexto do documento sem poder remover o "não invente,
    # use null", que é a garantia contra campo fabricado.
    messages = document_extract.build_messages(text, user)

    # `system` segue EXATAMENTE a regra do chat: com conteúdo, substitui o
    # prompt configurado; ausente ou vazio, vale o da chave (migration 0053)
    # ou, na falta dele, o da stack. Vazio não substitui nada — mesmo motivo
    # do chat: um system sem instrução não deve apagar a configuração da conta.
    #
    # Sem RAG, ao contrário do chat: aqui o contexto relevante é o
    # documento que acabou de ser enviado. Trechos da base de conhecimento
    # competiriam com ele e abririam espaço pro modelo preencher um campo
    # com dado de OUTRO documento — o oposto do que a extração promete.
    # Por isso não reaproveita build_stack_system_message, que traz os dois.
    system_text = (system or "").strip()
    if not system_text:
        stack, _ = resolve_key_stack(entry)
        system_text = resolve_system_prompt(entry, stack) or ""
    if system_text:
        messages.insert(0, {"role": "system", "content": system_text})

    payload = {
        "model": effective_model_name(stack_id, rewrite_model, machine),
        "messages": messages,
        # o Form já garante 0 < max_tokens <= CEILING; o clamp real contra a
        # janela do modelo vem do apply_context_budget abaixo
        "max_tokens": max_tokens or DOCUMENT_MAX_TOKENS,
        "stream": False,
        # temperatura 0: extração é determinística por natureza — o mesmo
        # documento com o mesmo schema deve dar o mesmo JSON. Sampling
        # aqui só produziria variação entre chamadas idênticas.
        "temperature": 0.0,
        # o que garante JSON aderente ao schema em vez de "JSON provável":
        # o vLLM converte isto em restrição de gramática. Não exige flag no
        # template — StructuredOutputsConfig.backend já é "auto" por default
        # na 0.24 (a tentativa de fixar o backend explicitamente quebrou o
        # boot dos pods; ver migration 0048).
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "document_extraction", "schema": parsed_schema},
        },
        # extração é tarefa fechada: o raciocínio custaria milhares de
        # tokens e segundos sem melhorar o resultado. E, nos planos com
        # thinking ligado, o bloco <think> disputaria com a gramática do
        # guided decoding — desligar remove o problema na origem em vez de
        # depender do filtro de saída (que ainda existe abaixo, por
        # segurança).
        "chat_template_kwargs": {"enable_thinking": False},
    }

    # O conteúdo enviado é a maior fatia do prompt, e um documento/imagem no
    # teto do plano passa fácil da janela do modelo. Sem isto o vLLM devolveria
    # 400 e o cliente veria o 502 genérico de "falha no modelo" — culpando
    # o servidor por um conteúdo grande demais. apply_context_budget clampa
    # o max_tokens ao que sobra e, se não sobrar espaço viável de resposta,
    # levanta ContextWindowExceeded (400 com mensagem explicando o
    # tamanho) — mesmo caminho e mesma mensagem do chat.
    est_tokens = estimate_prompt_tokens(messages)
    try:
        apply_context_budget(payload, machine, "max_tokens", est_tokens)
    except ContextWindowExceeded:
        log_gateway_request(**log_ctx, status_code=400, stream=False)
        # mensagem reescrita: a original é de chat ("comece uma sessão nova
        # com /clear", "configure CLAUDE_CODE_AUTO_COMPACT_WINDOW") e não
        # faz sentido nenhum pra quem subiu um documento/imagem — aqui o que
        # resolve é mandar menos conteúdo. Mesma exceção (mesmo status e
        # mesmo shape de corpo do exception handler), só o texto muda.
        # "página(s)" só entra pro caminho de PDF: imagem sempre tem pages=1
        # fixo, e citar "1 página" nesse caso seria um detalhe sem sentido
        # pra quem mandou uma imagem solta.
        content_desc = (
            f"o documento tem {pages} página(s) e ocupa"
            if path_label == "documents/extract"
            else "o conteúdo enviado ocupa"
        )
        raise ContextWindowExceeded(
            f"{content_desc} ~{est_tokens} tokens, o que não deixa espaço "
            f"para a resposta na janela de contexto deste plano "
            f"({machine.get('max_model_len')} tokens). Envie menos conteúdo por "
            "requisição (ex.: divida o documento) ou use um plano com janela maior."
        )

    try:
        upstream = await document_client.post(
            f"{machine['public_url']}/v1/chat/completions",
            json=payload,
            headers={"Authorization": authorization},
        )
    except httpx.HTTPError as e:
        logger.warning("%s: upstream indisponível para %s (%s)", path_label, flight_key, e)
        log_gateway_request(**log_ctx, status_code=503, stream=False)
        raise HTTPException(status_code=503, detail="máquina indisponível, tente novamente")

    if upstream.status_code != 200:
        # o corpo do vLLM pode citar a public_url do pod: fica no log do
        # servidor, o cliente recebe mensagem genérica (mesma disciplina
        # do proxy)
        logger.warning(
            "%s: vLLM respondeu %s (%s)",
            path_label, upstream.status_code, upstream.text[:500],
        )
        log_gateway_request(**log_ctx, status_code=502, stream=False)
        raise HTTPException(
            status_code=502, detail="falha ao processar o documento no modelo"
        )

    # 200 com corpo inesperado (não-JSON, ou sem choices) acontece: um proxy
    # no caminho devolvendo página de erro com status 200, ou o agent
    # respondendo algo fora do shape. Sem este guard viraria
    # ValueError/AttributeError → 500 cru e sem log nenhum.
    try:
        body = upstream.json()
        usage = body.get("usage")
        choice = (body.get("choices") or [{}])[0]
        content = choice.get("message", {}).get("content") or ""
        finish_reason = choice.get("finish_reason")
    except (ValueError, AttributeError, KeyError, IndexError, TypeError) as e:
        logger.warning(
            "%s: resposta fora do formato esperado (%s): %s",
            path_label, e, upstream.text[:300],
        )
        log_gateway_request(**log_ctx, status_code=502, stream=False)
        raise HTTPException(
            status_code=502, detail="resposta inesperada do modelo"
        )
    # rede de proteção: enable_thinking=False já deveria bastar, mas se um
    # template ignorar a flag o JSON vem depois de um </think> e o
    # json.loads falharia com um 502 enganoso.
    _, content = split_reasoning(content)

    # Truncado por teto de tokens: o JSON está pela metade e o validate
    # abaixo falharia com "não devolveu JSON aderente" — culpando o modelo
    # por um problema de espaço. Acontece justamente quando o
    # apply_context_budget clampou o max_tokens para caber num conteúdo
    # grande: sobra janela pro prompt, não pra resposta. Vale um erro
    # próprio porque a ação do cliente é diferente (dividir o conteúdo ou
    # enxugar o schema), não "tentar de novo".
    if finish_reason == "length":
        logger.warning(
            "%s: resposta truncada (max_tokens=%s, est_prompt=%s)",
            path_label, payload.get("max_tokens"), est_tokens,
        )
        log_gateway_request(**log_ctx, status_code=400, stream=False, usage=usage)
        raise HTTPException(
            status_code=400,
            detail={
                "message": (
                    "a resposta não caberia no espaço disponível: o conteúdo enviado "
                    "ocupa quase toda a janela de contexto do plano e não sobra lugar "
                    "para o JSON completo. Envie menos conteúdo por requisição ou "
                    "reduza o número de campos do schema."
                ),
                "max_tokens_disponivel": payload.get("max_tokens"),
                "raw_output": content[:2000],
            },
        )

    try:
        # to_thread: `parsed_schema` é do cliente e jsonschema compila
        # `pattern` com backtracking — ver validate_extraction_output.
        data = await asyncio.to_thread(
            validate_extraction_output, content, parsed_schema
        )
    # except largo de propósito: além de JSONDecodeError/ValidationError,
    # a resolução de referências levanta _WrappedReferencingError, que não
    # herda de nenhuma das duas. Estreitar aqui reintroduz o 500 opaco.
    except Exception as e:
        # com guided decoding ativo isto é raro (a gramática do vLLM não
        # deixaria sair JSON malformado), então quase sempre aponta pra
        # outra coisa: schema que o backend não suporta, ou resposta
        # interceptada no caminho. Devolver o texto cru é o que torna esse
        # diagnóstico possível do lado do cliente.
        logger.warning("%s: saída não aderente ao schema (%s)", path_label, e)
        log_gateway_request(**log_ctx, status_code=502, stream=False, usage=usage)
        raise HTTPException(
            status_code=502,
            detail={
                "message": "o modelo não devolveu JSON aderente ao schema",
                "reason": str(e)[:300],
                "raw_output": content[:2000],
            },
        )

    log_gateway_request(**log_ctx, status_code=200, stream=False, usage=usage)
    return {
        "data": data,
        "pages": pages,
        "ocr_used": ocr_used,
        "usage": usage,
    }


@app.post("/v1/documents/extract")
async def extract_document(
    request: Request,
    file: UploadFile = File(...),
    schema: str = Form(...),
    # mesmos papéis do /v1/chat/completions, só que em multipart (tem um
    # arquivo junto, então não dá pra ser JSON). A semântica é idêntica de
    # propósito: quem já integra com o chat não aprende regra nova.
    system: str | None = Form(None),
    user: str | None = Form(None),
    max_tokens: int | None = Form(None, gt=0, le=DOCUMENT_MAX_TOKENS_CEILING),
    authorization: str | None = Header(None),
):
    """Recebe um PDF e devolve o JSON aderente ao schema que o cliente mandou.

    É um dos dois endpoints do gateway que fazem trabalho de CPU próprio antes
    de chamar o pod (extração/OCR, ver document_extract.py e a rota irmã
    /v1/images/extract) — todo o resto é proxy. As duas consequências que o
    corpo abaixo respeita:

      * a extração roda em asyncio.to_thread, NUNCA inline: o gateway é um
        processo único e também atende todo o tráfego de chat, então OCR no
        event loop travaria as requisições de todos os outros clientes;
      * timeout próprio (document_client), porque extração + geração não
        cabe nos 60s do proxy_client de chat.

    Não passa por validate_body de propósito: aquele caminho existe pra
    sanear corpo de CLIENTE (system prompt, RAG, clamp de parâmetros, filtro
    de roles). Aqui o corpo é construído inteiro pelo servidor — o cliente
    não controla mensagens, sampling nem modelo. O único dado dele que entra
    no payload é o schema, e ele é validado antes."""
    started = time.monotonic()

    entry, key_hash, account_id, parsed_schema = await _authenticate_for_extraction(
        authorization, schema, request.headers, DOCUMENT_PATH
    )

    # Corte grosso ANTES de qualquer coisa cara: `file.size` vem do parser
    # multipart do Starlette (que já fez spool em disco), então dá pra recusar
    # um upload absurdo sem carregá-lo em RAM com file.read() e sem pagar o
    # resolve_route (ida ao banco, e que pode até religar um pod). O teto exato
    # do plano vem depois — aqui só barramos o que nenhum plano aceitaria.
    if file.size is not None and file.size > document_extract.max_limit_bytes():
        raise HTTPException(status_code=413, detail="documento excede o limite do serviço")

    # o teto de bytes é do PLANO, e o plano confiável vem de resolve_route
    # (key_plan resolvido dentro de _authenticate_for_extraction é só o da
    # chave, pode não ter stack resolvida)
    machine, rewrite_model, effective_plan, stack_id = await resolve_route(account_id, entry)
    await maybe_touch(stack_id, machine["id"])

    pdf_bytes = await file.read()
    try:
        document_extract.check_size(len(pdf_bytes), effective_plan)
    except document_extract.DocumentTooLarge as e:
        raise HTTPException(status_code=413, detail=str(e))

    flight_key = (stack_id, machine["id"])
    in_flight[flight_key] += 1
    check_concurrency(flight_key, machine, effective_plan)

    log_ctx = dict(
        account_id=account_id, stack_id=stack_id, api_key_id=entry["api_key_id"],
        machine_id=machine["id"], path=DOCUMENT_PATH,
        model=effective_model_name(stack_id, rewrite_model, machine),
        user_agent=request.headers.get("user-agent"), started=started,
    )

    try:
        try:
            # to_thread: o OCR é síncrono e pesado — ver o cabeçalho de
            # document_extract.py. Sem isso, uma página escaneada segura o
            # event loop e todo chat concorrente no gateway espera com ela.
            text, pages, ocr_used = await asyncio.to_thread(
                document_extract.extract_text, pdf_bytes, effective_plan
            )
        except document_extract.DocumentTooLarge as e:
            log_gateway_request(**log_ctx, status_code=413, stream=False)
            raise HTTPException(status_code=413, detail=str(e))
        except (document_extract.UnreadableDocument, document_extract.EmptyDocument) as e:
            log_gateway_request(**log_ctx, status_code=400, stream=False)
            raise HTTPException(status_code=400, detail=str(e))
        except document_extract.DocumentError as e:
            logger.warning("documents/extract: falha de extração (%s)", e)
            log_gateway_request(**log_ctx, status_code=500, stream=False)
            raise HTTPException(status_code=500, detail=str(e))

        return await _run_extraction_pipeline(
            text=text, pages=pages, ocr_used=ocr_used, parsed_schema=parsed_schema,
            user=user, system=system, entry=entry, stack_id=stack_id, machine=machine,
            rewrite_model=rewrite_model, max_tokens=max_tokens, authorization=authorization,
            flight_key=flight_key, log_ctx=log_ctx, path_label="documents/extract",
        )
    finally:
        # nunca vazar o contador: com in_flight preso em > 0 a auto-pausa da
        # máquina nunca mais dispararia
        release_flight(flight_key)


@app.post("/v1/images/extract")
async def extract_image(
    request: Request,
    file: UploadFile = File(...),
    schema: str = Form(...),
    system: str | None = Form(None),
    user: str | None = Form(None),
    max_tokens: int | None = Form(None, gt=0, le=DOCUMENT_MAX_TOKENS_CEILING),
    authorization: str | None = Header(None),
):
    """Recebe uma imagem (JPEG/PNG/WEBP) e devolve o JSON aderente ao schema
    que o cliente mandou. Irmã de /v1/documents/extract: mesmo contrato de
    resposta, mesma disciplina de OCR bloqueante em thread — ver
    document_extract.py e _run_extraction_pipeline acima.

    Ao contrário de PDF, aqui não existe "texto embutido" a extrair: a imagem
    inteira sempre passa por OCR (pytesseract), por isso `ocr_used` é sempre
    True e `pages` é sempre 1 na resposta. Não usa modelo vision/multimodal —
    o OCR roda no gateway (CPU), e o pod só recebe texto, exatamente como no
    caminho de PDF."""
    started = time.monotonic()

    entry, key_hash, account_id, parsed_schema = await _authenticate_for_extraction(
        authorization, schema, request.headers, IMAGE_PATH
    )

    # mesmo corte grosso do PDF, teto próprio de imagem (ver document_extract.py)
    if file.size is not None and file.size > document_extract.max_limit_image_bytes():
        raise HTTPException(status_code=413, detail="imagem excede o limite do serviço")

    machine, rewrite_model, effective_plan, stack_id = await resolve_route(account_id, entry)
    await maybe_touch(stack_id, machine["id"])

    image_bytes = await file.read()
    try:
        document_extract.check_image_size(len(image_bytes), effective_plan)
    except document_extract.DocumentTooLarge as e:
        raise HTTPException(status_code=413, detail=str(e))

    flight_key = (stack_id, machine["id"])
    in_flight[flight_key] += 1
    check_concurrency(flight_key, machine, effective_plan)

    log_ctx = dict(
        account_id=account_id, stack_id=stack_id, api_key_id=entry["api_key_id"],
        machine_id=machine["id"], path=IMAGE_PATH,
        model=effective_model_name(stack_id, rewrite_model, machine),
        user_agent=request.headers.get("user-agent"), started=started,
    )

    try:
        try:
            # to_thread: mesmo motivo do PDF — OCR é síncrono e pesado, não
            # pode travar o event loop compartilhado com o tráfego de chat.
            text, ocr_used = await asyncio.to_thread(
                document_extract.extract_text_from_image, image_bytes, effective_plan
            )
        except document_extract.DocumentTooLarge as e:
            log_gateway_request(**log_ctx, status_code=413, stream=False)
            raise HTTPException(status_code=413, detail=str(e))
        except (document_extract.UnreadableDocument, document_extract.EmptyDocument) as e:
            log_gateway_request(**log_ctx, status_code=400, stream=False)
            raise HTTPException(status_code=400, detail=str(e))
        except document_extract.DocumentError as e:
            logger.warning("images/extract: falha de extração (%s)", e)
            log_gateway_request(**log_ctx, status_code=500, stream=False)
            raise HTTPException(status_code=500, detail=str(e))

        return await _run_extraction_pipeline(
            text=text, pages=1, ocr_used=ocr_used, parsed_schema=parsed_schema,
            user=user, system=system, entry=entry, stack_id=stack_id, machine=machine,
            rewrite_model=rewrite_model, max_tokens=max_tokens, authorization=authorization,
            flight_key=flight_key, log_ctx=log_ctx, path_label="images/extract",
        )
    finally:
        release_flight(flight_key)


# ---------- Geração de PDF a partir de HTML ----------
#
# Mesma razão de ordenação de /v1/documents/extract acima: precisa estar
# registrado ANTES do catch-all /v1/{path:path}, senão "documents/generate"
# (que não está em ALLOWED_V1) viraria 404 sem explicação.
GENERATE_PATH = "documents/generate"

# Só usados no modo por prompt (quando `user` chama o modelo pra escrever o
# HTML) — mesmo papel de DOCUMENT_MAX_TOKENS/_CEILING no endpoint de extração.
GENERATION_MAX_TOKENS = int(os.environ.get("GENERATION_MAX_TOKENS", "8000"))
GENERATION_MAX_TOKENS_CEILING = int(os.environ.get("GENERATION_MAX_TOKENS_CEILING", "16000"))

# Sem `machine`/pod envolvido no modo direto (HTML já pronto), o
# in_flight/check_concurrency por máquina não protege esse caminho. Este
# semáforo é quem protege o único processo do Railway de N renders pesados
# simultâneos — vale para os dois modos, já que o custo de CPU do render é o
# mesmo independente de quem escreveu o HTML.
GENERATE_MAX_CONCURRENT = int(os.environ.get("GENERATE_MAX_CONCURRENT", "4"))
generate_semaphore = asyncio.Semaphore(GENERATE_MAX_CONCURRENT)


class GenerateDocumentRequest(BaseModel):
    # Exatamente um dos dois: `html` é o documento já pronto (modo direto,
    # sem modelo); `user` é a instrução do que gerar (modo por prompt, chama
    # o modelo). Validado no handler porque a mensagem de erro precisa
    # explicar a exclusividade — um Field de pydantic não expressaria isso
    # bem.
    html: str | None = None
    user: str | None = None
    # Só faz sentido junto de `user` (mesma semântica de /v1/documents/extract):
    # com conteúdo, substitui o system prompt da stack; ausente ou vazio, usa
    # o da stack.
    system: str | None = None
    max_tokens: int | None = Field(None, gt=0, le=GENERATION_MAX_TOKENS_CEILING)


async def _render_pdf_guarded(
    html: str, plan: str | None, log_ctx: dict, *, usage: dict | None = None
) -> bytes:
    """Semáforo de concorrência + render + tradução de erro — a parte comum
    aos dois modos de /v1/documents/generate. `usage` só é não-None no modo
    por prompt (é o consumo de tokens da chamada ao modelo que já aconteceu
    antes desta função)."""
    if generate_semaphore.locked():
        log_gateway_request(**log_ctx, status_code=429, stream=False, usage=usage)
        raise HTTPException(
            status_code=429, detail="muitas gerações de PDF simultâneas, tente novamente"
        )
    async with generate_semaphore:
        try:
            return await asyncio.to_thread(document_generate.render_pdf, html, plan)
        except document_generate.TooManyPages as e:
            log_gateway_request(**log_ctx, status_code=413, stream=False, usage=usage)
            raise HTTPException(status_code=413, detail=str(e))
        except document_generate.RenderError as e:
            log_gateway_request(**log_ctx, status_code=400, stream=False, usage=usage)
            raise HTTPException(status_code=400, detail=str(e))
        except document_generate.DocumentGenerateError as e:
            logger.warning("documents/generate: falha de geração (%s)", e)
            log_gateway_request(**log_ctx, status_code=500, stream=False, usage=usage)
            raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/documents/generate")
async def generate_document(
    request: Request,
    body: GenerateDocumentRequest,
    authorization: str | None = Header(None),
):
    """Devolve o PDF renderizado, em um de dois modos:

      * `html`: documento já pronto — conversão pura, sem inferência, sem
        pod. Funciona mesmo com a stack pausada e não gasta tokens.
      * `user` (+ `system` opcional): instrução do que gerar — o gateway
        chama o modelo pedindo HTML (mesma regra de composição system/user
        de /v1/documents/extract: `system` substitui o prompt da stack,
        `user` soma à instrução fixa de geração) e renderiza a resposta.

    Os dois modos são mutuamente exclusivos: misturar `html` com `user`
    seria ambíguo (qual documento vale?), e por isso é rejeitado explicitamente
    em vez de um dos dois ser ignorado em silêncio."""
    started = time.monotonic()

    if body.html and body.user:
        raise HTTPException(
            status_code=400,
            detail=(
                "informe `html` (documento já pronto) OU `user` (instrução para o "
                "modelo gerar o documento), não os dois"
            ),
        )
    if not body.html and not body.user:
        raise HTTPException(status_code=400, detail="informe `html` ou `user`")

    entry, key_hash = await authenticate(authorization, request.headers, GENERATE_PATH)
    stack, plan = resolve_key_stack(entry)
    check_rate_limit(key_hash, plan)
    account_id = entry["account_id"]

    # ---------- modo direto: HTML já pronto, sem modelo ----------
    if body.html:
        stack_id = (stack or {}).get("id")
        log_ctx = dict(
            account_id=account_id, stack_id=stack_id, api_key_id=entry["api_key_id"],
            machine_id=None, path=GENERATE_PATH, model=None,
            user_agent=request.headers.get("user-agent"), started=started,
        )
        try:
            document_generate.check_size(len(body.html.encode("utf-8")), plan)
        except document_generate.HtmlTooLarge as e:
            log_gateway_request(**log_ctx, status_code=413, stream=False)
            raise HTTPException(status_code=413, detail=str(e))

        pdf_bytes = await _render_pdf_guarded(body.html, plan, log_ctx)
        log_gateway_request(**log_ctx, status_code=200, stream=False)
        return Response(content=pdf_bytes, media_type="application/pdf")

    # ---------- modo por prompt: o modelo escreve o HTML ----------
    _, key_plan = resolve_key_stack(entry)
    await check_token_quota(account_id, key_plan, entry.get("purpose", "customer"))

    machine, rewrite_model, effective_plan, stack_id = await resolve_route(account_id, entry)
    await maybe_touch(stack_id, machine["id"])

    flight_key = (stack_id, machine["id"])
    in_flight[flight_key] += 1
    check_concurrency(flight_key, machine, effective_plan)

    log_ctx = dict(
        account_id=account_id, stack_id=stack_id, api_key_id=entry["api_key_id"],
        machine_id=machine["id"], path=GENERATE_PATH,
        model=effective_model_name(stack_id, rewrite_model, machine),
        user_agent=request.headers.get("user-agent"), started=started,
    )

    try:
        messages = document_generate.build_messages(body.user)

        # mesma regra do chat e de /v1/documents/extract: `system` com
        # conteúdo substitui o prompt configurado; ausente/vazio usa o da
        # chave (migration 0053) ou, na falta dele, o da stack.
        system_text = (body.system or "").strip()
        if not system_text:
            stack, _ = resolve_key_stack(entry)
            system_text = resolve_system_prompt(entry, stack) or ""
        if system_text:
            messages.insert(0, {"role": "system", "content": system_text})

        payload = {
            "model": effective_model_name(stack_id, rewrite_model, machine),
            "messages": messages,
            "max_tokens": body.max_tokens or GENERATION_MAX_TOKENS,
            "stream": False,
            # geração de documento não precisa de raciocínio visível: o
            # </think> só disputaria espaço com o HTML e seria descartado por
            # split_reasoning mesmo assim — desligar na origem evita o custo.
            "chat_template_kwargs": {"enable_thinking": False},
        }

        est_tokens = estimate_prompt_tokens(messages)
        try:
            apply_context_budget(payload, machine, "max_tokens", est_tokens)
        except ContextWindowExceeded:
            log_gateway_request(**log_ctx, status_code=400, stream=False)
            raise ContextWindowExceeded(
                f"a instrução ocupa ~{est_tokens} tokens, o que não deixa espaço "
                f"para a resposta na janela de contexto deste plano "
                f"({machine.get('max_model_len')} tokens). Envie uma instrução menor "
                "ou use um plano com janela maior."
            )

        try:
            upstream = await document_client.post(
                f"{machine['public_url']}/v1/chat/completions",
                json=payload,
                headers={"Authorization": authorization},
            )
        except httpx.HTTPError as e:
            logger.warning(
                "documents/generate: upstream indisponível para %s (%s)", flight_key, e
            )
            log_gateway_request(**log_ctx, status_code=503, stream=False)
            raise HTTPException(status_code=503, detail="máquina indisponível, tente novamente")

        if upstream.status_code != 200:
            logger.warning(
                "documents/generate: vLLM respondeu %s (%s)",
                upstream.status_code, upstream.text[:500],
            )
            log_gateway_request(**log_ctx, status_code=502, stream=False)
            raise HTTPException(status_code=502, detail="falha ao gerar o documento no modelo")

        try:
            resp_body = upstream.json()
            usage = resp_body.get("usage")
            choice = (resp_body.get("choices") or [{}])[0]
            content = choice.get("message", {}).get("content") or ""
            finish_reason = choice.get("finish_reason")
        except (ValueError, AttributeError, KeyError, IndexError, TypeError) as e:
            logger.warning(
                "documents/generate: resposta fora do formato esperado (%s): %s",
                e, upstream.text[:300],
            )
            log_gateway_request(**log_ctx, status_code=502, stream=False)
            raise HTTPException(status_code=502, detail="resposta inesperada do modelo")

        # rede de proteção: enable_thinking=False já deveria bastar, mas se um
        # template ignorar a flag o HTML vem depois de um </think>.
        _, content = split_reasoning(content)
        # rede de proteção nº 2: modelo devolveu ```html ... ``` em volta do
        # HTML apesar da instrução — ver document_generate.strip_html_fences.
        content = document_generate.strip_html_fences(content)

        if finish_reason == "length":
            logger.warning(
                "documents/generate: resposta truncada (max_tokens=%s, est_prompt=%s)",
                payload.get("max_tokens"), est_tokens,
            )
            log_gateway_request(**log_ctx, status_code=400, stream=False, usage=usage)
            raise HTTPException(
                status_code=400,
                detail={
                    "message": (
                        "o HTML gerado foi cortado: a instrução ocupa quase toda a janela "
                        "de contexto do plano e não sobrou espaço para a resposta completa. "
                        "Envie uma instrução menor ou aumente max_tokens."
                    ),
                    "max_tokens_disponivel": payload.get("max_tokens"),
                    "raw_output": content[:2000],
                },
            )

        # o HTML devolvido pelo modelo passa pelo MESMO teto de bytes do modo
        # direto: nada garante que o modelo respeitou a instrução de tamanho,
        # e render_pdf ainda tem o teto de páginas como segunda linha de defesa.
        try:
            document_generate.check_size(len(content.encode("utf-8")), effective_plan)
        except document_generate.HtmlTooLarge as e:
            log_gateway_request(**log_ctx, status_code=413, stream=False, usage=usage)
            raise HTTPException(status_code=413, detail=str(e))

        pdf_bytes = await _render_pdf_guarded(content, effective_plan, log_ctx, usage=usage)
        log_gateway_request(**log_ctx, status_code=200, stream=False, usage=usage)
        return Response(content=pdf_bytes, media_type="application/pdf")
    finally:
        # nunca vazar o contador: com in_flight preso em > 0 a auto-pausa da
        # máquina nunca mais dispararia
        release_flight(flight_key)


@app.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request, authorization: str | None = Header(None)):
    started = time.monotonic()
    allowed_methods = ALLOWED_V1.get(path)
    if not allowed_methods or request.method not in allowed_methods:
        raise HTTPException(status_code=404, detail="not found")

    # lido cedo (antes de autenticar/resolver rota) pra rejeitar corpo grande
    # sem pagar o custo de wake/provisionamento numa request que será recusada
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="corpo da requisição excede o limite")

    entry, key_hash = await authenticate(authorization, request.headers, path)
    account_id = entry["account_id"]
    _, key_plan = resolve_key_stack(entry)
    check_rate_limit(key_hash, key_plan)
    await check_token_quota(account_id, key_plan, entry.get("purpose", "customer"))

    machine, rewrite_model, effective_plan, stack_id = await resolve_route(account_id, entry)
    await maybe_touch(stack_id, machine["id"])

    # incrementa ANTES dos awaits lentos (leitura do body, embeddings do RAG):
    # o grace recheck da auto-pausa conta este in_flight — quanto mais cedo,
    # menor a janela pra pausa derrubar a máquina com request já resolvido
    flight_key = (stack_id, machine["id"])
    in_flight[flight_key] += 1
    check_concurrency(flight_key, machine, effective_plan)

    log_ctx = dict(
        account_id=account_id, stack_id=stack_id, api_key_id=entry["api_key_id"],
        machine_id=machine["id"], path=path,
        model=effective_model_name(stack_id, rewrite_model, machine),
        user_agent=request.headers.get("user-agent"), started=started,
    )

    body_json = None
    try:
        if body:
            try:
                body_json = json.loads(body)
                # validate_body (chat/completions/embeddings) ou
                # validate_responses_body (Codex, formato Responses) travam
                # o model, aplicam piso/teto de tokens e clamp de parâmetros,
                # e injetam system prompt da stack + RAG no formato certo
                if path == "responses":
                    body_json = await validate_responses_body(
                        body_json, entry, rewrite_model, machine, stack_id
                    )
                else:
                    body_json = await validate_body(
                        body_json, entry, rewrite_model, machine, stack_id
                    )
                body = json.dumps(body_json).encode()
            except HTTPException:
                raise  # rejeição explícita (ex.: limite de mensagens) não pode virar "segue como está"
            except Exception:
                pass  # body não-JSON segue como está

        # O read do httpx aqui é só a rede de segurança EXTERNA: quem controla o
        # prazo é o watchdog de duas fases em aiter_bytes_watchdog, que sabe
        # separar prefill de silêncio no meio do stream. A folga de 15s garante
        # que seja sempre o watchdog a responder primeiro, com mensagem útil e
        # com o 504 no log, em vez do httpx com um ReadTimeout cru.
        #
        # Sem `timeout` aqui esta request herdava os 60s do proxy_client, que
        # durante o prefill (zero byte fluindo) matavam justamente a request de
        # prompt grande — ver CHAT_STREAM_TTFT_TIMEOUT_S.
        # connect/write/pool continuam os do client (5s/10s/10s): só o read muda.
        is_stream_request = isinstance(body_json, dict) and body_json.get("stream") is True
        upstream_req = proxy_client.build_request(
            request.method,
            f"{machine['public_url']}/v1/{path}",
            content=body,
            headers={
                # repassa a Bearer original: o agent valida e conta uso por chave
                "Authorization": authorization,
                "Content-Type": request.headers.get("content-type", "application/json"),
            },
            timeout=httpx.Timeout(
                (CHAT_STREAM_TTFT_TIMEOUT_S + 15.0)
                if is_stream_request
                # sem stream o vLLM não manda byte nenhum até a geração inteira
                # fechar, então o read tem que cobrir a geração — mesmo motivo
                # (e mesmo teto) do MESSAGES_NONSTREAM_TIMEOUT_S.
                else MESSAGES_NONSTREAM_TIMEOUT_S,
                connect=5.0, write=10.0, pool=10.0,
            ),
        )
        upstream = await proxy_client.send(upstream_req, stream=True)
    except httpx.HTTPError as e:
        release_flight(flight_key)
        # detalhe da exceção (pode conter a public_url interna do pod) só no
        # log do servidor — o cliente recebe uma mensagem genérica
        logger.warning("proxy: upstream indisponível para %s (%s)", flight_key, e)
        raise HTTPException(status_code=503, detail="máquina indisponível, tente novamente")
    except BaseException:
        # cliente desconectou no meio do body (CancelledError) ou qualquer
        # outra falha — nunca vazar o contador, senão a máquina fica com
        # in_flight > 0 pra sempre e a auto-pausa nunca mais dispara
        release_flight(flight_key)
        raise

    if path == "models":
        # lista também os adapters LoRA carregados na máquina ("acct-<uuid>")
        # — sem filtrar, qualquer tenant autenticado enumerava os account_id
        # de TODOS os outros tenants que dividem o mesmo pod. O agent conta
        # esta chamada em usage_metrics como qualquer request autenticada;
        # registra-la também em gateway_requests mantém o total da página da
        # máquina coerente com o histórico de Requisições.
        try:
            raw = await upstream.aread()
            try:
                payload = json.loads(raw)
                payload["data"] = [
                    m for m in payload.get("data", [])
                    if not str(m.get("id", "")).startswith("acct-")
                ]
                raw = json.dumps(payload).encode()
            except Exception:
                pass
        finally:
            await upstream.aclose()
            release_flight(flight_key)
            log_gateway_request(
                **log_ctx, status_code=upstream.status_code,
                stream=False, usage=None,
            )
        return Response(
            content=raw,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )

    filter_reasoning = path == "chat/completions" and effective_plan in REASONING_LEAK_PLANS
    # is_stream_request já foi calculado antes do build_request — é ele que
    # decide o read timeout do upstream (streaming mede prefill, não-streaming
    # mede a geração inteira).

    if filter_reasoning and not is_stream_request:
        usage = None
        try:
            raw = await upstream.aread()
            try:
                payload = json.loads(raw)
                usage = payload.get("usage")
                for choice in payload.get("choices", []):
                    message = choice.get("message")
                    if isinstance(message, dict) and isinstance(message.get("content"), str):
                        reasoning, visible = split_reasoning(message["content"])
                        if reasoning is not None:
                            message["content"] = visible
                raw = json.dumps(payload).encode()
            except Exception:
                pass  # resposta não é o JSON de chat completion esperado -> repassa como veio
        finally:
            await upstream.aclose()
            release_flight(flight_key)
            log_gateway_request(**log_ctx, status_code=upstream.status_code, stream=False, usage=usage)
        return Response(
            content=raw,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )

    if filter_reasoning:
        return StreamingResponse(
            filtered_reasoning_stream(upstream, flight_key, log_ctx),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "text/event-stream"),
        )

    async def stream_and_release():
        # requisição não-streamed (embeddings, completions sem stream, etc.):
        # o corpo cabe todo em memória (mesmo custo já pago pelos branches de
        # "models" e do filtro de reasoning acima via aread()) — acumula pra
        # extrair "usage" no fim sem mudar o mecanismo de resposta.
        collect_full = not is_stream_request
        full = b"" if collect_full else None
        # streaming de verdade: o scanner só faz json.loads na linha rara que
        # carrega "usage" (o chunk final do chat, o evento response.completed
        # do Responses) — os deltas de conteúdo nunca chegam a ser parseados.
        scanner = SseUsageScanner()
        usage = None
        status_code = upstream.status_code
        try:
            # watchdog só no streaming de verdade. Numa request NÃO-streamed o
            # "primeiro chunk" só chega quando a geração inteira fecha, então um
            # teto de TTFT aqui mataria geração longa e legítima — lá quem manda
            # é o read do httpx (MESSAGES_NONSTREAM_TIMEOUT_S, ver build_request).
            async for chunk in aiter_bytes_watchdog(
                upstream,
                ttft_s=CHAT_STREAM_TTFT_TIMEOUT_S if is_stream_request else 0,
                idle_s=CHAT_STREAM_IDLE_TIMEOUT_S if is_stream_request else 0,
                log_label=str(flight_key),
            ):
                yield chunk
                if collect_full:
                    full += chunk
                    continue
                scanner.feed(chunk)
        except UpstreamStreamTimeout as e:
            # mesmo raciocínio do filtered_reasoning_stream: o 200 já foi junto
            # com o cabeçalho, então o erro só cabe no corpo — e o log tem que
            # registrar 504, senão a falha some do gateway_requests.
            status_code = 504
            if is_stream_request:
                yield b"data: " + json.dumps({
                    "error": {
                        "message": (
                            "a máquina não entregou resposta a tempo "
                            f"({e.phase}, {e.waited:.0f}s) — tente novamente ou "
                            "reduza o tamanho do prompt"
                        ),
                        "type": "upstream_timeout",
                        "code": "upstream_timeout",
                    },
                }).encode() + b"\n\n"
                yield b"data: [DONE]\n\n"
        finally:
            if collect_full:
                try:
                    usage = usage_from_event(json.loads(full)) if full else None
                except Exception:
                    usage = None
            else:
                usage = scanner.finish()
            await upstream.aclose()
            release_flight(flight_key)
            log_gateway_request(
                **log_ctx, status_code=status_code,
                stream=is_stream_request, usage=usage,
            )

    return StreamingResponse(
        stream_and_release(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


# ---------- Demo pública da landing page ----------
#
# A única rota do gateway sem autenticação e a única chamada de um browser. Ela
# atende o terminal da hero do trystac.com, que prova que a inferência é real
# em vez de animar uma resposta gravada.
#
# O que a mantém segura NÃO é uma credencial (não há) e nem CORS (contornável
# com curl), e sim o formato do endpoint: um único campo de entrada, 200
# caracteres, 80 tokens de saída, system prompt fixo, pod dedicado, 5 requests
# por hora por IP e um teto global por hora. A política toda vive em demo.py,
# testada em test_demo.py; aqui fica o I/O.


async def assert_demo_pod_is_dedicated() -> None:
    """Desliga a demo se DEMO_UPSTREAM_URL for a URL de uma máquina do pool.

    O requisito "pod dedicado" é arquitetural — o caminho do /demo não passa por
    resolve_route, então ele nunca ESCOLHE uma máquina de cliente. O que sobra é
    o erro de configuração: alguém colar no DEMO_UPSTREAM_URL a public_url de um
    pod que já serve stacks. Aí o tráfego anônimo passaria a disputar as vagas
    de sequência de quem paga, sem aparecer em check_concurrency (que conta por
    stack/máquina, e a demo não tem stack).

    Checagem no boot e não por request: a lista de máquinas muda em minutos, a
    env var só muda em deploy. Falha de leitura do Supabase não desliga a demo —
    ficar sem a vitrine por um blip de rede seria pior que o risco que a
    checagem cobre, e o log fica com o aviso.
    """
    global demo_enabled
    if not demo_enabled:
        missing = [
            name
            for name, value in (
                ("DEMO_UPSTREAM_URL", DEMO_UPSTREAM_URL),
                ("DEMO_UPSTREAM_KEY", DEMO_UPSTREAM_KEY),
                ("DEMO_MODEL", DEMO_MODEL),
            )
            if not value
        ]
        logger.info("demo pública desligada — faltando %s", ", ".join(missing))
        return
    if not DEMO_ALLOWED_ORIGINS:
        logger.warning(
            "demo pública ligada sem DEMO_ALLOWED_ORIGINS — nenhum browser vai "
            "conseguir chamar /demo (fail-closed de CORS)"
        )
    try:
        machines = await supa.list_machines_with_pod()
    except Exception as e:
        logger.warning("demo pública: não deu pra conferir o pod dedicado (%s)", e)
        return
    for machine in machines:
        url = (machine.get("public_url") or "").rstrip("/")
        if url and url == DEMO_UPSTREAM_URL:
            demo_enabled = False
            logger.error(
                "demo pública DESLIGADA: DEMO_UPSTREAM_URL aponta para a máquina "
                "%s, que atende stacks de cliente. A demo exige um pod dedicado.",
                machine.get("id"),
            )
            return
    logger.info("demo pública ligada — pod dedicado, modelo %s", DEMO_MODEL)


def demo_cors_or_403(request: Request, *, preflight: bool = False) -> dict[str, str]:
    headers = demo.cors_headers(
        request.headers.get("origin"), DEMO_ALLOWED_ORIGINS, preflight=preflight
    )
    if headers is None:
        raise HTTPException(status_code=403, detail="origem não autorizada")
    return headers


def demo_error(
    status_code: int, detail: str, cors: dict[str, str], **headers: str
) -> JSONResponse:
    """Erro do /demo COM os headers de CORS.

    Necessário porque o CORSMiddleware global está fechado (allow_origins=[]) e
    quem emite os headers desta rota é ela mesma. Sem eles na resposta de erro, o
    browser bloqueia a leitura e o `fetch` do terminal rejeita com um erro de
    CORS no console em vez de receber o 400/429 — o resultado visível na hero é o
    mesmo (volta pra animação), mas o motivo real fica invisível pra quem for
    depurar."""
    return JSONResponse(
        status_code=status_code, content={"detail": detail}, headers={**cors, **headers}
    )


def demo_rate_limited(request: Request, cors: dict[str, str]) -> JSONResponse | None:
    """Rate limit por IP + teto global. Devolve o 429 (com Retry-After) ou None.

    O IP entra hasheado: o dict vive até 1h em memória e não há razão pra
    guardar endereço em claro pra contar 5 requests. Sem IP identificável
    (nenhum dos headers de proxy, sem client) todos caem no MESMO bucket — o
    lado seguro do fail-closed, porque a alternativa (liberar) transformaria
    "esconder o IP" na forma de furar o limite.
    """
    raw_ip = client_ip(request.headers) or (
        request.client.host if request.client else None
    )
    key = (
        hashlib.sha256(raw_ip.encode()).hexdigest()[:32] if raw_ip else "sem-ip"
    )
    # sequencial e não os dois de uma vez: `take` CONSOME a vaga, então avaliar
    # os dois juntos gastaria uma vaga do teto global numa request que o limite
    # por IP já recusou
    for limiter, bucket in ((demo_ip_limiter, key), (demo_global_limiter, "global")):
        retry_after = limiter.take(bucket)
        if retry_after is not None:
            return demo_error(
                429,
                "limite da demo excedido, tente novamente mais tarde",
                cors,
                **{"Retry-After": str(int(retry_after))},
            )
    return None


@app.options("/demo")
async def demo_preflight(request: Request):
    if not demo_enabled:
        raise HTTPException(status_code=404, detail="not found")
    return Response(status_code=204, headers=demo_cors_or_403(request, preflight=True))


@app.post("/demo")
async def demo_infer(request: Request):
    if not demo_enabled:
        # 404 e não 503: a rota simplesmente não existe neste deploy, e anunciar
        # "existe mas está fora" só convida a insistir.
        raise HTTPException(status_code=404, detail="not found")

    cors = demo_cors_or_403(request)

    # corte pelo Content-Length ANTES de ler o corpo: o corpo legítimo tem ~250
    # bytes (200 caracteres + envelope JSON), e sem isso um Content-Length
    # gigante seria lido inteiro pra RAM antes de qualquer validação — mesma
    # lógica do middleware reject_oversized_upload, aqui inline porque é uma
    # rota só.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > 4096:
        return demo_error(400, "corpo da requisição excede o limite", cors)

    body = await request.body()
    if len(body) > 4096:
        return demo_error(400, "corpo da requisição excede o limite", cors)
    try:
        payload_in = json.loads(body)
    except Exception:
        return demo_error(400, "corpo não é JSON válido", cors)
    if not isinstance(payload_in, dict):
        return demo_error(400, "corpo não é um objeto JSON", cors)

    # Validação ANTES do rate limit, de propósito: um prompt de 201 caracteres
    # não pode queimar uma das 5 tentativas da pessoa. Request inválida custa
    # parsing de 250 bytes e nada de GPU, então não precisa da proteção.
    try:
        prompt = demo.validate_prompt(payload_in.get("prompt"))
    except demo.InvalidPrompt as e:
        return demo_error(400, str(e), cors)

    limited = demo_rate_limited(request, cors)
    if limited is not None:
        return limited

    # `demo.build_payload` monta o corpo do zero: nada de payload_in além do
    # texto chega ao pod. max_tokens, model, temperature e system não são
    # sequer lidos do que o cliente mandou.
    try:
        upstream = await demo_client.send(
            demo_client.build_request(
                "POST",
                f"{DEMO_UPSTREAM_URL}/v1/chat/completions",
                json=demo.build_payload(prompt, DEMO_MODEL),
            ),
            stream=True,
        )
    except httpx.HTTPError as e:
        logger.warning("demo: pod indisponível (%s)", e)
        return demo_error(503, "demo indisponível", cors)

    # o status é conferido ANTES de devolver o StreamingResponse: depois do
    # primeiro byte não há mais como mudar de status, e o terminal da hero
    # depende de receber um erro HTTP pra voltar pra animação em silêncio.
    if upstream.status_code != 200:
        detail = (await upstream.aread())[:500]
        await upstream.aclose()
        logger.warning(
            "demo: pod respondeu %s (%s)", upstream.status_code, detail.decode(errors="replace")
        )
        # 502 genérico: o corpo do erro do pod pode carregar a URL interna dele e
        # o nome do modelo — vai pro log, nunca pro browser
        return demo_error(502, "demo indisponível", cors)

    logger.info(
        "demo: %s caracteres · rede %s",
        len(prompt),
        # bloco /24, nunca o endereço completo — a mesma disciplina de privacidade
        # de client_identity.network_bucket no resto do gateway
        network_bucket(request.headers) or "?",
    )

    async def stream():
        visible = demo.VisibleText()
        try:
            async for chunk in upstream.aiter_bytes():
                for text in visible.feed(chunk):
                    yield demo.sse_delta(text)
            for text in visible.finish():
                yield demo.sse_delta(text)
            yield demo.SSE_DONE
        except httpx.HTTPError:
            # o pod cortou no meio: não há o que dizer ao cliente (o stream já
            # começou com status 200) e não há contador pra liberar — a demo
            # nunca toca in_flight. O terminal da hero trata resposta truncada
            # como fim de stream.
            #
            # CancelledError (visitante fechou a aba) NÃO é capturada de
            # propósito: engolir cancelamento deixa a task pendurada. O finally
            # abaixo fecha o upstream nos dois caminhos.
            pass
        finally:
            await upstream.aclose()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            **cors,
            "Cache-Control": "no-store",
            # o buffer do proxy reverso engoliria o streaming e a resposta
            # apareceria de uma vez no fim — o efeito de "token a token" na
            # hero morre aí
            "X-Accel-Buffering": "no",
        },
    )


# ---------- Health e admin ----------


@app.get("/")
async def root():
    return {"ok": True, "service": "gateway"}


@app.get("/health")
async def health():
    return {"ok": True, "uptime_s": time.time() - STARTED_AT}


@app.get("/admin/routes")
async def admin_routes(x_admin_secret: str | None = Header(None)):
    require_admin(x_admin_secret)
    in_flight_by_machine: dict[str, int] = defaultdict(int)
    for (_, m), n in in_flight.items():
        in_flight_by_machine[m] += n
    now = time.time()
    return {
        "in_flight": {f"{a}@{m}": n for (a, m), n in in_flight.items() if n > 0},
        # agregado por máquina — o número que importa pra ver o teto elástico
        # de check_concurrency em ação (compara com machines.max_concurrent_seqs)
        "in_flight_by_machine": {m: n for m, n in in_flight_by_machine.items() if n > 0},
        "key_cache_size": len(key_cache),
        # travas em memória → há quantos segundos cada uma está setada. Idade
        # acima do TTL respectivo (RECREATE/PROVISION/KEY_SYNC_LOCK_TTL_S) é uma
        # trava vazada sendo tolerada até o próximo request expirá-la — antes
        # dava pra diagnosticar isso só reiniciando o processo.
        "provisioning_in_progress": {p: round(now - ts, 1) for p, ts in provisioning_in_progress.items()},
        "recreating_in_progress": {m: round(now - ts, 1) for m, ts in recreating_in_progress.items()},
        "key_sync_in_progress": {m: round(now - ts, 1) for m, ts in key_sync_in_progress.items()},
        "pending_recreates": sorted(pending_recreates),
    }


@app.post("/admin/flush-key-cache")
async def flush_key_cache(x_admin_secret: str | None = Header(None)):
    require_admin(x_admin_secret)
    n = len(key_cache)
    key_cache.clear()
    # client_seen junto: quem libera um ambiente no painel espera reconectar
    # na hora, e o 403 fica cacheado por CLIENT_TOUCH_THROTTLE_S (5 min) sem
    # este clear. O painel já chama este endpoint em releaseStackClient.
    clients = len(client_seen)
    client_seen.clear()
    return {"ok": True, "flushed": n, "client_stacks_flushed": clients}


@app.post("/admin/sync-machine-keys")
async def admin_sync_machine_keys(request: Request, x_admin_secret: str | None = Header(None)):
    """Agenda o reenvio das chaves da máquina quando o pod ficar saudável.
    Chamado pelo painel após o startMachine (o pod religa com o agent zerado
    e o poll de saúde precisa viver num processo longo — este aqui, não numa
    função serverless)."""
    require_admin(x_admin_secret)
    body = await request.json()
    machine_id = body.get("machine_id")
    if not machine_id:
        raise HTTPException(status_code=400, detail="machine_id é obrigatório")
    _forget_machine_upserts(machine_id)
    schedule_key_sync(machine_id)
    return {"ok": True, "scheduled": True}


@app.post("/admin/migrate")
async def admin_migrate(request: Request, x_admin_secret: str | None = Header(None)):
    require_admin(x_admin_secret)
    body = await request.json()
    stack_id = body.get("stack_id")
    target = body.get("target_machine_id")
    if not stack_id or not target:
        raise HTTPException(status_code=400, detail="stack_id e target_machine_id são obrigatórios")
    try:
        return await lifecycle_mgr.migrate(stack_id, target)
    except MigrationError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@app.post("/admin/reap-idle")
async def admin_reap_idle(x_admin_secret: str | None = Header(None)):
    """Dispara um ciclo do idle reaper manualmente (útil em teste)."""
    require_admin(x_admin_secret)
    reaped = await lifecycle_mgr.reap_idle_once()
    return {"ok": True, "reaped": reaped}


@app.post("/admin/billing-reconcile")
async def admin_billing_reconcile(x_admin_secret: str | None = Header(None)):
    """Dispara um ciclo de reconciliação de billing manualmente (útil em
    teste: evita esperar até 60s pra ver um past_due vencido virar
    suspended)."""
    require_admin(x_admin_secret)
    suspended, released = await billing_reconcile_once()
    return {"ok": True, "suspended": suspended, "slots_released": released}


@app.post("/admin/consolidate")
async def admin_consolidate(x_admin_secret: str | None = Header(None)):
    """Dispara um ciclo de consolidação manualmente (útil em teste)."""
    require_admin(x_admin_secret)
    moved = await lifecycle_mgr.consolidate_once()
    return {"ok": True, "moved": moved}


@app.post("/admin/stop-idle-machines")
async def admin_stop_idle_machines(x_admin_secret: str | None = Header(None)):
    """Dispara um ciclo de auto-pausa manualmente (útil em teste)."""
    require_admin(x_admin_secret)
    stopped = await lifecycle_mgr.stop_idle_machines_once()
    return {"ok": True, "stopped": stopped}


@app.post("/admin/ensure-capacity")
async def admin_ensure_capacity(x_admin_secret: str | None = Header(None)):
    """Dispara um ciclo de reposição proativa manualmente (útil em teste e
    chamado pelo painel na hora em que o interruptor liga, pra não esperar
    até 5min pelo próximo tick automático)."""
    require_admin(x_admin_secret)
    triggered = await lifecycle_mgr.ensure_capacity_once()
    return {"ok": True, "triggered_plans": triggered}
