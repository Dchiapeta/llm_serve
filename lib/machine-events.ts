// Envelope do originador de um evento de máquina (migration 0070).
//
// Espelho de docker/gateway/trigger_ctx.py: a allowlist é a MESMA, e fechada
// pelo mesmo motivo — só metadado, nunca conteúdo de prompt nem chave em
// claro. O gateway manda o envelope no corpo de /api/machines/provision e
// /api/machines/[id]/recreate; as rotas filtram por aqui antes de gravar.
// Módulo puro (sem next/headers) pra ser importável de rota e de componente.

import type { TriggerEnvelope } from "./types"

export const TRIGGER_META_KEYS = [
  "actor",
  "cause",
  "trace_id",
  "account_id",
  "account_name",
  "api_key_id",
  "key_prefix",
  "stack_id",
  "stack_slug",
  "plan",
  "category",
  "purpose",
  "path",
  "user_agent",
  "reason",
  "admin_email",
  "machine_id",
  "machine_name",
] as const

// Teto do envelope serializado. Um originador legítimo tem ~400 bytes; o
// limite existe pra que um gateway comprometido não use a coluna jsonb como
// depósito.
export const MAX_TRIGGER_BYTES = 2048

// Metadado que acompanha um logEvent (lib/actions.ts).
export type EventMeta = {
  cause?: string
  trigger?: TriggerEnvelope
  machineLabel?: string | null
}

/**
 * Filtra um envelope vindo de fora (corpo HTTP). Devolve null se não for um
 * objeto, se estourar o teto ou se não sobrar nada após a allowlist — o
 * chamador trata null como "sem envelope" (gateway antigo), nunca como erro.
 */
export function parseTriggerEnvelope(raw: unknown): TriggerEnvelope | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null
  const out: Record<string, string> = {}
  for (const key of TRIGGER_META_KEYS) {
    const value = (raw as Record<string, unknown>)[key]
    if (typeof value === "string" && value.length > 0) out[key] = value
  }
  if (Object.keys(out).length === 0) return null
  if (JSON.stringify(out).length > MAX_TRIGGER_BYTES) return null
  return out as TriggerEnvelope
}
