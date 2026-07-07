// Comunicação painel → agent rodando dentro do pod (porta 8000, rota /admin).
// Autenticado pelo admin_secret gerado por máquina.

import type { Machine } from "./types"

export type AgentKeyEntry = {
  key_hash: string
  key_prefix: string
  account_name: string
}

export type AgentKeyMetrics = {
  requests: number
  tokens_in: number
  tokens_out: number
  last_used: number | null
}

export type AgentMetricsSnapshot = {
  per_key: Record<string, AgentKeyMetrics>
  total_requests: number
  concurrent_now: number
  concurrent_peak: number
  uptime_s: number
}

async function agentFetch<T>(
  machine: Pick<Machine, "public_url" | "admin_secret">,
  path: string,
  init?: RequestInit & { json?: unknown; timeoutMs?: number }
): Promise<T> {
  if (!machine.public_url) throw new Error("Máquina sem URL pública")
  const { json, timeoutMs, ...rest } = init ?? {}
  const res = await fetch(`${machine.public_url}/admin${path}`, {
    ...rest,
    headers: {
      "X-Admin-Secret": machine.admin_secret,
      ...(json !== undefined ? { "Content-Type": "application/json" } : {}),
      ...rest.headers,
    },
    body: json !== undefined ? JSON.stringify(json) : rest.body,
    cache: "no-store",
    signal: AbortSignal.timeout(timeoutMs ?? 15_000),
  })
  if (!res.ok) {
    throw new Error(`Agent ${path} → ${res.status}: ${await res.text()}`)
  }
  return res.json() as Promise<T>
}

export const agent = {
  syncKeys: (m: Pick<Machine, "public_url" | "admin_secret">, keys: AgentKeyEntry[]) =>
    agentFetch<{ ok: boolean; count: number }>(m, "/sync-keys", {
      method: "POST",
      json: { keys },
    }),
  logs: (
    m: Pick<Machine, "public_url" | "admin_secret">,
    opts?: { keyPrefix?: string; tail?: number }
  ) => {
    const params = new URLSearchParams()
    if (opts?.keyPrefix) params.set("key_prefix", opts.keyPrefix)
    if (opts?.tail) params.set("tail", String(opts.tail))
    const qs = params.toString()
    return agentFetch<{ lines: string[] }>(m, `/logs${qs ? `?${qs}` : ""}`)
  },
  health: (m: Pick<Machine, "public_url" | "admin_secret">) =>
    agentFetch<{ ok: boolean; model: string }>(m, "/health"),
  // reset=true zera os contadores no agent após a leitura (coleta por delta).
  metrics: (
    m: Pick<Machine, "public_url" | "admin_secret">,
    opts?: { reset?: boolean }
  ) =>
    agentFetch<AgentMetricsSnapshot>(
      m,
      `/metrics${opts?.reset ? "?reset=true" : ""}`,
      { timeoutMs: 5_000 }
    ),
}
