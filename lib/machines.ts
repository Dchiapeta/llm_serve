import { runpod, type RunPodPod } from "./runpod"
import { createSupabaseAdmin } from "./supabase/server"
import type { Machine } from "./types"

type SupabaseAdmin = ReturnType<typeof createSupabaseAdmin>

// Status exibido na UI: além dos status do banco, "starting" indica que o
// pod está de pé mas o vLLM ainda não respondeu (baixando/carregando modelo)
// e "failed" que o processo do vLLM morreu (ex.: OOM no boot) com o pod vivo.
export type MachineDisplayStatus = Machine["status"] | "starting" | "failed"

// Sonda o /health público do agent para saber se o vLLM já está servindo.
// Também sonda máquinas em "creating": se o agent já responde, o pod está de
// pé mesmo que a reconciliação com o RunPod ainda não tenha promovido o banco.
export async function machineDisplayStatus(m: Machine): Promise<MachineDisplayStatus> {
  if (m.status !== "running" && m.status !== "creating") return m.status
  if (!m.public_url) return m.status
  try {
    const res = await fetch(`${m.public_url}/health`, {
      cache: "no-store",
      signal: AbortSignal.timeout(3_000),
    })
    if (!res.ok) return m.status === "creating" ? "creating" : "starting"
    const body = (await res.json()) as {
      ok?: boolean
      vllm_ready?: boolean
      vllm_alive?: boolean
    }
    // agents antigos não reportam vllm_ready; se respondem, consideramos pronto
    if (body.vllm_ready !== false) return "running"
    // vLLM não responde E o processo morreu = crash (agents antigos não
    // reportam vllm_alive e continuam caindo em "starting")
    return body.vllm_alive === false ? "failed" : "starting"
  } catch {
    // agent inacessível: pod ainda puxando imagem / subindo (ou, em
    // "creating", nem alocado ainda)
    return m.status === "creating" ? "creating" : "starting"
  }
}

// Mapeia o desiredStatus do RunPod para o status interno da máquina.
const POD_STATUS_MAP: Record<string, Machine["status"]> = {
  RUNNING: "running",
  EXITED: "stopped",
  TERMINATED: "terminated",
}

// Compara o status do banco com o estado real do RunPod e grava as
// divergências. Retorna a lista já atualizada (inclusive terminadas, que o
// chamador pode filtrar). Se a API do RunPod falhar, mantém o banco intacto.
export async function reconcileMachineStatuses(
  machines: Machine[],
  db?: SupabaseAdmin
): Promise<Machine[]> {
  if (machines.length === 0) return machines

  const pods = await runpod.listPods().catch(() => null)
  if (!pods) return machines

  const client = db ?? createSupabaseAdmin()
  const podById = new Map<string, RunPodPod>(pods.map((p) => [p.id, p]))
  const updates: Array<{ id: string; status: Machine["status"]; cost: number | null }> = []

  const reconciled = machines.map((m) => {
    if (!m.runpod_pod_id) return m
    const pod = podById.get(m.runpod_pod_id)

    // Máquina ainda subindo pode não aparecer na API por instantes; não a
    // marcamos como terminada para evitar falso positivo.
    if (!pod) {
      if (m.status === "creating") return m
      return { ...m, status: "terminated" as const }
    }

    const status = POD_STATUS_MAP[pod.desiredStatus] ?? m.status
    const cost = pod.costPerHr ?? m.cost_per_hr
    return { ...m, status, cost_per_hr: cost }
  })

  for (let i = 0; i < machines.length; i++) {
    const before = machines[i]
    const after = reconciled[i]
    if (before.status !== after.status || before.cost_per_hr !== after.cost_per_hr) {
      updates.push({ id: after.id, status: after.status, cost: after.cost_per_hr })
    }
  }

  if (updates.length > 0) {
    await Promise.all(
      updates.map((u) =>
        client
          .from("machines")
          .update({ status: u.status, cost_per_hr: u.cost })
          .eq("id", u.id)
      )
    )
  }

  return reconciled
}
