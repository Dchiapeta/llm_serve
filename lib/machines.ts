import { runpod, type RunPodPod } from "./runpod"
import { createSupabaseAdmin } from "./supabase/server"
import type { Machine } from "./types"

type SupabaseAdmin = ReturnType<typeof createSupabaseAdmin>

// Nome com que o vLLM SERVE o modelo — é o que o cliente manda no campo
// "model" da request. Com "--served-model-name" (no VLLM_EXTRA_ARGS do env
// ou no start command do template) o vLLM aceita SÓ o alias, não o repo HF;
// sem a flag, serve pelo próprio MODEL_NAME. Aceita as formas "--flag nome"
// e "--flag=nome"; com múltiplos nomes, o primeiro é o canônico.
export function parseServedModelName(args: string | null | undefined): string | null {
  if (!args) return null
  const m = args.match(/--served-model-name[=\s]+(\S+)/)
  return m ? m[1] : null
}

// Janela de contexto do vLLM (--max-model-len, no VLLM_EXTRA_ARGS do env ou no
// start command). O gateway guarda na máquina para clampar max_tokens ao
// orçamento restante da janela. Null = template sem a flag (vLLM usa a janela
// nativa do config do modelo, que não conhecemos aqui).
export function parseMaxModelLen(args: string | null | undefined): number | null {
  if (!args) return null
  const m = args.match(/--max-model-len[=\s]+(\d+)/)
  return m ? Number(m[1]) : null
}

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
  const updates: Array<{
    id: string
    status: Machine["status"]
    cost: number | null
    fromStatus: Machine["status"]
  }> = []

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
      updates.push({
        id: after.id,
        status: after.status,
        cost: after.cost_per_hr,
        fromStatus: before.status,
      })
    }
  }

  if (updates.length > 0) {
    // compare-and-set no status: o after.status foi decidido a partir de
    // before.status + snapshot do RunPod. Se outro escritor (server action ou o
    // reconciler do gateway) mudou o status nesse meio-tempo, não sobrescreve
    // (o .eq no status casa 0 linhas). Fecha a corrida do finding #7. O custo
    // segue sem guarda — é métrica, não estado de transição.
    await Promise.all(
      updates.map((u) =>
        client
          .from("machines")
          .update({ status: u.status, cost_per_hr: u.cost })
          .eq("id", u.id)
          .eq("status", u.fromStatus)
      )
    )
  }

  return reconciled
}
