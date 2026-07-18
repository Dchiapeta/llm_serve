// Comunicação painel → agent rodando dentro do pod (porta 8000, rota /admin).
// Autenticado pelo admin_secret gerado por máquina.

import type { Machine } from "./types"

export type AgentKeyEntry = {
  key_hash: string
  // identificador estável pra logs/métricas por-chave no agent — key_prefix
  // sozinho (32 bits) tem colisão possível entre chaves diferentes
  api_key_id: string
  key_prefix: string
  account_name: string
  expires_at: string | null
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

// Arquivo de adapter LoRA a baixar no pod: nome local + signed URL do storage.
export type LoraSignedFile = {
  name: string
  url: string
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
    opts?: { apiKeyId?: string; tail?: number }
  ) => {
    const params = new URLSearchParams()
    if (opts?.apiKeyId) params.set("api_key_id", opts.apiKeyId)
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
  // Insere/atualiza chaves sem limpar as existentes (usado pelo gateway na
  // alocação/migração — o sync-keys continua sendo o fluxo do painel).
  upsertKeys: (m: Pick<Machine, "public_url" | "admin_secret">, keys: AgentKeyEntry[]) =>
    agentFetch<{ ok: boolean; count: number }>(m, "/upsert-keys", {
      method: "POST",
      json: { keys },
    }),
  // Baixa o adapter (signed URLs) e carrega no vLLM em runtime.
  // Timeout largo: download do storage + load em VRAM podem levar minutos.
  loadLora: (
    m: Pick<Machine, "public_url" | "admin_secret">,
    body: { lora_name: string; files: LoraSignedFile[] }
  ) =>
    agentFetch<{
      ok: boolean
      lora_name: string
      download_s: number
      load_s: number
      already_loaded?: boolean
    }>(m, "/load-lora", { method: "POST", json: body, timeoutMs: 120_000 }),
  unloadLora: (m: Pick<Machine, "public_url" | "admin_secret">, loraName: string) =>
    agentFetch<{ ok: boolean; lora_name: string }>(m, "/unload-lora", {
      method: "POST",
      json: { lora_name: loraName },
      timeoutMs: 30_000,
    }),
  listLoras: (m: Pick<Machine, "public_url" | "admin_secret">) =>
    agentFetch<{ loras: string[] }>(m, "/loras", { timeoutMs: 10_000 }),
}
