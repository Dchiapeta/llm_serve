// Coleta de uso: puxa os contadores acumulados no agent de cada máquina
// (zerando-os na leitura) e grava o delta como uma janela em usage_metrics.
// Best-effort — agent fora do ar ou subindo não bloqueia o carregamento da
// página; os contadores seguem acumulando no pod até a próxima coleta.

import { agent } from "./agent"
import { createSupabaseAdmin } from "./supabase/server"
import type { Machine } from "./types"

type SupabaseAdmin = ReturnType<typeof createSupabaseAdmin>

export async function collectUsageMetrics(
  machines: Machine[],
  db?: SupabaseAdmin
): Promise<void> {
  const targets = machines.filter((m) => m.status === "running" && m.public_url)
  if (targets.length === 0) return

  const client = db ?? createSupabaseAdmin()
  const { data: keysData } = await client
    .from("api_keys")
    .select("id, machine_id, key_prefix")
    .in(
      "machine_id",
      targets.map((m) => m.id)
    )

  const keyIdByMachineAndPrefix = new Map<string, string>()
  for (const k of keysData ?? []) {
    keyIdByMachineAndPrefix.set(`${k.machine_id}:${k.key_prefix}`, k.id)
  }

  await Promise.all(
    targets.map(async (m) => {
      try {
        const snap = await agent.metrics(m, { reset: true })
        const windowStart = new Date().toISOString()
        const rows = Object.entries(snap.per_key)
          .filter(([, v]) => v.requests > 0)
          .map(([prefix, v]) => ({
            api_key_id: keyIdByMachineAndPrefix.get(`${m.id}:${prefix}`) ?? null,
            machine_id: m.id,
            window_start: windowStart,
            requests: v.requests,
            tokens_in: v.tokens_in,
            tokens_out: v.tokens_out,
            concurrent_peak: snap.concurrent_peak,
          }))
        if (rows.length > 0) {
          await client.from("usage_metrics").insert(rows)
        }
      } catch {
        // agent inacessível — os contadores continuam no pod para a próxima
      }
    })
  )
}
