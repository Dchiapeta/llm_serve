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

// Teto de sequências concorrentes do vLLM (--max-num-seqs). O gateway lê isso
// de machines.max_concurrent_seqs (migration 0028) em check_concurrency para
// decidir quando devolver 429. Sem esta função a coluna nunca era preenchida
// por ninguém e TODA máquina caía no DEFAULT_MAX_CONCURRENT_SEQS do gateway —
// o valor real do template não chegava lá, então mexer no --max-num-seqs de um
// template não tinha efeito nenhum no teto aplicado. Null = template sem a
// flag (aí o fallback do gateway continua valendo, e é o certo).
export function parseMaxNumSeqs(args: string | null | undefined): number | null {
  if (!args) return null
  const m = args.match(/--max-num-seqs[=\s]+(\d+)/)
  return m ? Number(m[1]) : null
}

// Máximo de imagens por prompt que o pod aceita (--limit-mm-per-prompt). O
// gateway usa isso para RECORTAR imagem excedente antes de enviar: sem o
// recorte o vLLM devolve 400, e como clientes agênticos reenviam a conversa
// inteira a cada turno, esse 400 se repete pra sempre e mata a sessão (ver
// docker/gateway/content_policy.py).
//
// A flag aceita duas formas, ambas tratadas aqui:
//   {"image":4,"video":0}                 (contagem)
//   {"image":{"count":4,"width":512}}     (com opções)
// Sem a flag o vLLM assume 999 por modalidade (config/multimodal.py), e é isso
// que devolvemos — não null — pra que o gateway saiba que imagem é permitida.
// Null fica reservado pra "não sei" (template sem VLLM_EXTRA_ARGS nenhum), onde
// o gateway não recorta nada.
export function parseImageLimit(args: string | null | undefined): number | null {
  if (!args) return null
  const m = args.match(/--limit-mm-per-prompt[=\s]+(\S+)/)
  if (!m) return 999 // flag ausente: default do vLLM
  try {
    const parsed = JSON.parse(m[1]) as Record<string, unknown>
    const image = parsed.image
    if (typeof image === "number") return image
    if (image && typeof image === "object") {
      const count = (image as Record<string, unknown>).count
      if (typeof count === "number") return count
    }
    // flag presente mas sem falar de imagem: outras modalidades limitadas,
    // imagem segue no default
    return 999
  } catch {
    return null // JSON malformado: não arriscar recortar com base em palpite
  }
}

// Teto de gerações em voo do pod de difusão (IMAGE_QUEUE_CAPACITY, lido em
// docker/image/server.py). Ocupa o mesmo lugar do --max-num-seqs do vLLM: é o
// número que o gateway usa pra decidir quando devolver 429, e vem do `env` do
// template porque um pod de imagem não tem linha de comando de vLLM nenhuma.
//
// Null quando ausente ou ilegível — que é o que o gateway espera pra cair no
// fallback dele. Um palpite aqui seria pior que não saber: alto demais empurra
// carga que o pod recusa, baixo demais desperdiça a máquina.
export function parseImageQueueCapacity(
  env: Record<string, string> | null | undefined
): number | null {
  const raw = env?.IMAGE_QUEUE_CAPACITY
  if (!raw) return null
  const n = Number(raw)
  return Number.isInteger(n) && n > 0 ? n : null
}

// Campos de `machines` derivados das flags do vLLM no template. O gateway lê
// tudo da própria máquina (não busca o template a cada request), então essas
// colunas são uma cópia denormalizada que precisa ser reescrita SEMPRE que o
// pod sobe com um template — no provisionamento e na recriação. Sem isso, uma
// edição do template só chegava ao gateway criando uma máquina nova: recriar
// não bastava, e o caso do --served-model-name era o pior (o pod passa a servir
// o alias novo enquanto o gateway continua fixando o antigo → 404 em toda
// request). Aceita env.VLLM_EXTRA_ARGS ou start_command, nessa ordem.
export function vllmFlagsFromTemplate(tpl: {
  env: Record<string, string> | null
  start_command: string | null
}) {
  const extra = tpl.env?.VLLM_EXTRA_ARGS
  const cmd = tpl.start_command
  return {
    // alias servido pelo vLLM (--served-model-name), pra o gateway fixar o
    // "model" correto em vez do path do HF; null se o template não usa a flag
    served_model_name: parseServedModelName(extra) ?? parseServedModelName(cmd),
    // janela de contexto (--max-model-len), pro gateway clampar max_tokens
    // ao orçamento restante; null se o template não usa a flag
    max_model_len: parseMaxModelLen(extra) ?? parseMaxModelLen(cmd),
    // teto de sequências concorrentes (--max-num-seqs), pro gateway aplicar
    // o 429 no valor REAL do deploy em vez do fallback conservador dele.
    //
    // O pod de DIFUSÃO não tem --max-num-seqs (não roda vLLM): o teto dele é o
    // IMAGE_QUEUE_CAPACITY, o total de gerações em voo que a fila admite
    // (docker/image/policy.py). Sem este fallback a coluna ficava NULL, o
    // gateway caía no DEFAULT_MAX_CONCURRENT_SEQS de 8 e deixava passar o dobro
    // do que o pod aceita — o excedente voltava como 429 queue_full do pod, e
    // não como o 429 com Retry-After do gateway.
    max_concurrent_seqs:
      parseMaxNumSeqs(extra) ?? parseMaxNumSeqs(cmd) ?? parseImageQueueCapacity(tpl.env),
    // teto de imagens por prompt (--limit-mm-per-prompt), pro gateway
    // recortar excedente em vez de deixar o pod devolver 400
    max_images_per_prompt: parseImageLimit(extra) ?? parseImageLimit(cmd),
  }
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

    const mapped = POD_STATUS_MAP[pod.desiredStatus] ?? m.status
    // Máquina recém-criada pode reportar EXITED por instantes antes de o
    // container subir — não a rebaixamos para "stopped" (apareceria pausada
    // durante o boot). Só RUNNING a promove e TERMINATED a encerra; espelha o
    // guard do pod ausente acima.
    const status = m.status === "creating" && mapped === "stopped" ? "creating" : mapped
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
