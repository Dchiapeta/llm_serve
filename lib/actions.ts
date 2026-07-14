"use server"

import { revalidatePath } from "next/cache"
import { redirect } from "next/navigation"
import { randomBytes } from "crypto"

import { agent, type AgentKeyEntry, type LoraSignedFile } from "./agent"
import { computeCapacity, vramSlots } from "./capacity"
import { generateHexKey, hashKey, keyPrefix } from "./keys"
import { getClientLocation, setClientLocation } from "./routing"
import { generateStackSlug, STACK_SLUG_RE } from "./slug"
import { listGpuTypes, podProxyUrl, runpod } from "./runpod"
import { createSupabaseAdmin, createSupabaseServerClient } from "./supabase/server"
import { TEMPLATE_PLANS, type ApiKey, type LoraAdapter, type Machine, type Stack, type Template, type TemplatePlan } from "./types"

// ---------- Auth ----------

export async function login(formData: FormData) {
  const supabase = await createSupabaseServerClient()
  const { error } = await supabase.auth.signInWithPassword({
    email: String(formData.get("email")),
    password: String(formData.get("password")),
  })
  if (error) redirect(`/login?error=${encodeURIComponent(error.message)}`)
  redirect("/")
}

export async function signup(formData: FormData) {
  const email = String(formData.get("email"))
  const password = String(formData.get("password"))
  const confirm = String(formData.get("confirm"))

  if (password !== confirm) {
    redirect(`/signup?error=${encodeURIComponent("As senhas não coincidem")}`)
  }

  const supabase = await createSupabaseServerClient()
  const { data, error } = await supabase.auth.signUp({ email, password })
  if (error) redirect(`/signup?error=${encodeURIComponent(error.message)}`)

  // Quando a confirmação de e-mail está ativa, não há sessão ainda.
  if (!data.session) {
    redirect(
      `/login?message=${encodeURIComponent(
        "Conta criada. Verifique seu e-mail para confirmar o acesso."
      )}`
    )
  }
  redirect("/")
}

export async function logout() {
  const supabase = await createSupabaseServerClient()
  await supabase.auth.signOut()
  redirect("/login")
}

// ---------- Helpers ----------

async function logEvent(machineId: string | null, type: string, message: string) {
  const db = createSupabaseAdmin()
  await db.from("machine_events").insert({ machine_id: machineId, type, message })
}

// Plano/tier do template: VibeCoder, Pro, Max ou Enterprise.
function parsePlan(formData: FormData): TemplatePlan {
  const raw = String(formData.get("plan") || "").trim()
  if (!TEMPLATE_PLANS.includes(raw as TemplatePlan)) {
    throw new Error("Plano inválido")
  }
  return raw as TemplatePlan
}

// Quantidade de GPUs por máquina do template; padrão 1.
function parseGpuCount(formData: FormData): number {
  const raw = String(formData.get("gpu_count") ?? "").trim()
  if (!raw) return 1
  const n = Number(raw)
  if (!Number.isInteger(n) || n < 1) {
    throw new Error("Quantidade de GPUs deve ser um número inteiro maior que zero")
  }
  return n
}

// Teto manual de usuários: campo opcional; vazio = sem teto (só VRAM).
function parseMaxUsers(formData: FormData): number | null {
  const raw = String(formData.get("max_users") ?? "").trim()
  if (!raw) return null
  const n = Number(raw)
  if (!Number.isInteger(n) || n < 1) {
    throw new Error("Máx. de usuários deve ser um número inteiro maior que zero")
  }
  return n
}

// Converte o "Container start command" (texto multilinha) em argv para o RunPod.
// Cada token separado por espaço/quebra de linha vira um argumento.
function parseStartCommand(raw: string | null | undefined): string[] {
  return (raw ?? "").split(/\s+/).filter(Boolean)
}

// Lista de portas informada como texto (separadas por vírgula/espaço) → array.
function parsePortList(raw: string | null | undefined): string[] {
  return (raw ?? "")
    .split(/[\s,]+/)
    .map((s) => s.trim())
    .filter(Boolean)
}

// Formato de portas do RunPod: "8000/http", "22/tcp".
function toRunpodPorts(httpPorts: string[], tcpPorts: string[]): string[] {
  return [
    ...httpPorts.map((p) => `${p}/http`),
    ...tcpPorts.map((p) => `${p}/tcp`),
  ]
}

// ---------- Templates ----------

export async function createTemplate(formData: FormData) {
  const db = createSupabaseAdmin()
  const name = String(formData.get("name"))
  const image = String(formData.get("image"))
  const modelName = String(formData.get("model_name"))
  const plan = parsePlan(formData)
  const diskGb = Number(formData.get("disk_gb") || 40)
  const footprint = Number(formData.get("model_footprint_gb") || 16)
  const kvReserve = Number(formData.get("kv_reserve_gb_per_user") || 2)
  const loraFootprint = Number(formData.get("lora_footprint_gb") || 0.5)
  const maxUsers = parseMaxUsers(formData)
  const gpuCount = parseGpuCount(formData)
  const startCommand = String(formData.get("start_command") || "").trim() || null
  const volumeGb = Number(formData.get("volume_gb") || 0)
  const volumeMountPath =
    String(formData.get("volume_mount_path") || "").trim() || "/workspace"
  const httpPorts = parsePortList(String(formData.get("http_ports") || ""))
  const tcpPorts = parsePortList(String(formData.get("tcp_ports") || ""))
  const gpuTypes = formData
    .getAll("gpu_types")
    .map((s) => String(s).trim())
    .filter(Boolean)

  let env: Record<string, string> = {}
  try {
    env = JSON.parse(String(formData.get("env") || "{}"))
  } catch {
    throw new Error("Env vars devem ser um JSON válido")
  }

  // cria o template também no RunPod para aparecer no console deles
  let runpodTemplateId: string | null = null
  try {
    const created = await runpod.createTemplate({
      name,
      imageName: image,
      containerDiskInGb: diskGb,
      volumeInGb: volumeGb,
      volumeMountPath,
      env,
      ports: toRunpodPorts(httpPorts, tcpPorts),
      dockerStartCmd: parseStartCommand(startCommand),
    })
    runpodTemplateId = created.id
  } catch (e) {
    console.error("Falha ao criar template no RunPod:", e)
  }

  const { error } = await db.from("templates").insert({
    runpod_template_id: runpodTemplateId,
    name,
    image,
    model_name: modelName,
    plan,
    gpu_types: gpuTypes,
    gpu_count: gpuCount,
    env,
    start_command: startCommand,
    disk_gb: diskGb,
    volume_gb: volumeGb,
    volume_mount_path: volumeMountPath,
    http_ports: httpPorts,
    tcp_ports: tcpPorts,
    model_footprint_gb: footprint,
    kv_reserve_gb_per_user: kvReserve,
    lora_footprint_gb: loraFootprint,
    max_users: maxUsers,
  })
  if (error) throw new Error(error.message)
  revalidatePath("/templates")
}

// Importa um template que já existe no RunPod, criando o registro local
// que aponta para ele. Os campos de capacidade (modelo/footprint) são
// informados pelo usuário, pois o RunPod não os armazena.
export async function importTemplate(formData: FormData) {
  const db = createSupabaseAdmin()
  const runpodTemplateId = String(formData.get("runpod_template_id"))
  if (!runpodTemplateId) throw new Error("Template do RunPod não informado")

  // busca os dados autoritativos direto do RunPod
  const templates = await runpod.listTemplates()
  const remote = templates.find((t) => t.id === runpodTemplateId)
  if (!remote) throw new Error("Template não encontrado no RunPod")

  const modelName = String(formData.get("model_name"))
  const plan = parsePlan(formData)
  const footprint = Number(formData.get("model_footprint_gb") || 16)
  const kvReserve = Number(formData.get("kv_reserve_gb_per_user") || 2)
  const loraFootprint = Number(formData.get("lora_footprint_gb") || 0.5)
  const maxUsers = parseMaxUsers(formData)
  const gpuCount = parseGpuCount(formData)
  const gpuTypes = formData
    .getAll("gpu_types")
    .map((s) => String(s).trim())
    .filter(Boolean)

  const remotePorts = remote.ports ?? []
  const httpPorts = remotePorts
    .filter((p) => p.endsWith("/http"))
    .map((p) => p.replace("/http", ""))
  const tcpPorts = remotePorts
    .filter((p) => p.endsWith("/tcp"))
    .map((p) => p.replace("/tcp", ""))

  const { error } = await db.from("templates").insert({
    runpod_template_id: remote.id,
    name: remote.name,
    image: remote.imageName,
    model_name: modelName,
    plan,
    gpu_types: gpuTypes,
    gpu_count: gpuCount,
    env: remote.env ?? {},
    start_command: (remote.dockerStartCmd ?? []).join(" ") || null,
    disk_gb: remote.containerDiskInGb ?? 40,
    volume_gb: remote.volumeInGb ?? 0,
    volume_mount_path: remote.volumeMountPath || "/workspace",
    http_ports: httpPorts,
    tcp_ports: tcpPorts,
    model_footprint_gb: footprint,
    kv_reserve_gb_per_user: kvReserve,
    lora_footprint_gb: loraFootprint,
    max_users: maxUsers,
  })
  if (error) throw new Error(error.message)
  revalidatePath("/templates")
}

export async function updateTemplate(formData: FormData) {
  const db = createSupabaseAdmin()
  const id = String(formData.get("id"))
  if (!id) throw new Error("Produto não informado")

  const name = String(formData.get("name"))
  const image = String(formData.get("image"))
  const modelName = String(formData.get("model_name"))
  const plan = parsePlan(formData)
  const diskGb = Number(formData.get("disk_gb") || 40)
  const footprint = Number(formData.get("model_footprint_gb") || 16)
  const kvReserve = Number(formData.get("kv_reserve_gb_per_user") || 2)
  const loraFootprint = Number(formData.get("lora_footprint_gb") || 0.5)
  const maxUsers = parseMaxUsers(formData)
  const gpuCount = parseGpuCount(formData)
  const startCommand = String(formData.get("start_command") || "").trim() || null
  const volumeGb = Number(formData.get("volume_gb") || 0)
  const volumeMountPath =
    String(formData.get("volume_mount_path") || "").trim() || "/workspace"
  const httpPorts = parsePortList(String(formData.get("http_ports") || ""))
  const tcpPorts = parsePortList(String(formData.get("tcp_ports") || ""))
  const gpuTypes = formData
    .getAll("gpu_types")
    .map((s) => String(s).trim())
    .filter(Boolean)

  let env: Record<string, string> = {}
  try {
    env = JSON.parse(String(formData.get("env") || "{}"))
  } catch {
    throw new Error("Env vars devem ser um JSON válido")
  }

  const { data: tpl } = await db.from("templates").select("*").eq("id", id).single<Template>()

  // reflete a alteração no RunPod quando o template está sincronizado
  if (tpl?.runpod_template_id) {
    try {
      await runpod.updateTemplate(tpl.runpod_template_id, {
        name,
        imageName: image,
        containerDiskInGb: diskGb,
        volumeInGb: volumeGb,
        volumeMountPath,
        env,
        ports: toRunpodPorts(httpPorts, tcpPorts),
        dockerStartCmd: parseStartCommand(startCommand),
      })
    } catch (e) {
      console.error("Falha ao atualizar template no RunPod:", e)
    }
  }

  const { error } = await db
    .from("templates")
    .update({
      name,
      image,
      model_name: modelName,
      plan,
      gpu_types: gpuTypes,
      gpu_count: gpuCount,
      env,
      start_command: startCommand,
      disk_gb: diskGb,
      volume_gb: volumeGb,
      volume_mount_path: volumeMountPath,
      http_ports: httpPorts,
      tcp_ports: tcpPorts,
      model_footprint_gb: footprint,
      kv_reserve_gb_per_user: kvReserve,
      lora_footprint_gb: loraFootprint,
      max_users: maxUsers,
    })
    .eq("id", id)
  if (error) throw new Error(error.message)
  revalidatePath("/templates")
}

export async function deleteTemplate(id: string) {
  const db = createSupabaseAdmin()
  const { data: tpl } = await db.from("templates").select("*").eq("id", id).single()
  if (tpl?.runpod_template_id) {
    try {
      await runpod.deleteTemplate(tpl.runpod_template_id)
    } catch (e) {
      console.error("Falha ao deletar template no RunPod:", e)
    }
  }
  await db.from("templates").delete().eq("id", id)
  revalidatePath("/templates")
}

// ---------- Machines ----------

// Núcleo de provisionamento: cria o pod no RunPod + registro em machines.
// Sem redirect — reutilizado por createMachine (painel) e createStack.
async function provisionMachine(input: {
  name: string
  templateId: string
  gpuTypeId: string
  maxUsers?: number | null // null/undefined = usar o padrão do template
}): Promise<{ machineId: string } | { error: string }> {
  const db = createSupabaseAdmin()
  const { name, templateId, gpuTypeId } = input

  const { data: tpl, error: tplErr } = await db
    .from("templates")
    .select("*")
    .eq("id", templateId)
    .single<Template>()
  if (tplErr || !tpl) return { error: "Produto não encontrado" }

  // teto manual: valor informado, com fallback para o padrão do template
  const maxUsers = input.maxUsers ?? tpl.max_users

  const adminSecret = randomBytes(24).toString("hex")

  const gpus = await listGpuTypes()
  const gpu = gpus.find((g) => g.id === gpuTypeId)
  const gpuCount = tpl.gpu_count ?? 1
  const totalVramGb = gpu?.memoryInGb != null ? gpu.memoryInGb * gpuCount : null

  if (maxUsers !== null && totalVramGb != null) {
    const cap = vramSlots({
      vramGb: totalVramGb,
      modelFootprintGb: tpl.model_footprint_gb,
      kvReserveGbPerUser: tpl.kv_reserve_gb_per_user,
    })
    if (maxUsers > cap) {
      return {
        error: `A GPU ${gpu?.displayName ?? gpuTypeId} comporta no máximo ${cap} usuário(s) para este modelo (pedido: ${maxUsers})`,
      }
    }
  }

  const startCmd = parseStartCommand(tpl.start_command)
  const ports = toRunpodPorts(tpl.http_ports ?? [], tpl.tcp_ports ?? [])
  let pod: Awaited<ReturnType<typeof runpod.createPod>>
  try {
    pod = await runpod.createPod({
      name,
      imageName: tpl.image,
      gpuTypeIds: [gpuTypeId],
      gpuCount,
      containerDiskInGb: tpl.disk_gb,
      volumeInGb: tpl.volume_gb ?? 0,
      volumeMountPath: tpl.volume_mount_path || "/workspace",
      ports: ports.length > 0 ? ports : ["8000/http"],
      cloudType: "SECURE",
      // só sobrescreve o entrypoint da imagem quando o template define um comando
      ...(startCmd.length > 0 ? { dockerStartCmd: startCmd } : {}),
      env: {
        ...tpl.env,
        MODEL_NAME: tpl.model_name,
        AGENT_ADMIN_SECRET: adminSecret,
        GPU_COUNT: String(gpuCount),
        ...(maxUsers !== null ? { MAX_USERS: String(maxUsers) } : {}),
      },
    })
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e)
    // Falta de estoque no RunPod é um erro esperado — devolve mensagem amigável
    if (msg.includes("no instances currently available")) {
      return {
        error: `Sem GPUs ${gpu?.displayName ?? gpuTypeId} disponíveis no RunPod agora. Tente outro tipo de GPU ou novamente em alguns minutos.`,
      }
    }
    return { error: `Falha ao criar o pod no RunPod: ${msg}` }
  }

  const { data: machine, error } = await db
    .from("machines")
    .insert({
      runpod_pod_id: pod.id,
      name,
      gpu_type:
        gpuCount > 1
          ? `${gpu?.displayName ?? gpuTypeId} ×${gpuCount}`
          : gpu?.displayName ?? gpuTypeId,
      status: "creating",
      template_id: tpl.id,
      admin_secret: adminSecret,
      model_name: tpl.model_name,
      vram_gb: totalVramGb,
      cost_per_hr: pod.costPerHr ?? null,
      public_url: podProxyUrl(pod.id, 8000),
      max_users: maxUsers,
    })
    .select()
    .single<Machine>()
  if (error) {
    return {
      error: `Pod ${pod.id} criado no RunPod, mas falhou ao salvar no banco: ${error.message}`,
    }
  }

  await logEvent(machine.id, "created", `Máquina "${name}" criada (${gpu?.displayName ?? gpuTypeId})`)
  revalidatePath("/machines")
  return { machineId: machine.id }
}

export async function createMachine(formData: FormData) {
  const result = await provisionMachine({
    name: String(formData.get("name")),
    templateId: String(formData.get("template_id")),
    gpuTypeId: String(formData.get("gpu_type")),
    maxUsers: parseMaxUsers(formData),
  })
  if ("error" in result) return { error: result.error }
  redirect(`/machines/${result.machineId}`)
}

export async function refreshMachineStatus(machineId: string) {
  const db = createSupabaseAdmin()
  const { data: m } = await db.from("machines").select("*").eq("id", machineId).single<Machine>()
  if (!m?.runpod_pod_id) return
  try {
    const pod = await runpod.getPod(m.runpod_pod_id)
    const statusMap: Record<string, Machine["status"]> = {
      RUNNING: "running",
      EXITED: "stopped",
      TERMINATED: "terminated",
    }
    const status = statusMap[pod.desiredStatus] ?? m.status
    await db
      .from("machines")
      .update({ status, cost_per_hr: pod.costPerHr ?? m.cost_per_hr })
      .eq("id", machineId)
    // Pod acabou de ficar pronto: empurra chaves emitidas durante o boot
    // (createStack pode criar a chave com a máquina ainda em "creating").
    if (m.status !== "running" && status === "running") {
      await syncMachineKeys(machineId).catch((e) =>
        console.error("Sync pós-boot falhou (tenta de novo no próximo refresh):", e)
      )
    }
  } catch (e) {
    const msg = String(e)
    if (msg.includes("404")) {
      await db.from("machines").update({ status: "terminated" }).eq("id", machineId)
    }
  }
  revalidatePath(`/machines/${machineId}`)
  revalidatePath("/machines")
}

export async function stopMachine(machineId: string) {
  const db = createSupabaseAdmin()
  const { data: m } = await db.from("machines").select("*").eq("id", machineId).single<Machine>()
  if (!m?.runpod_pod_id) throw new Error("Máquina sem pod associado")
  await runpod.stopPod(m.runpod_pod_id)
  await db.from("machines").update({ status: "stopped" }).eq("id", machineId)
  await logEvent(machineId, "stopped", `Máquina "${m.name}" desativada`)
  revalidatePath(`/machines/${machineId}`)
  revalidatePath("/machines")
}

export async function startMachine(machineId: string) {
  const db = createSupabaseAdmin()
  const { data: m } = await db.from("machines").select("*").eq("id", machineId).single<Machine>()
  if (!m?.runpod_pod_id) throw new Error("Máquina sem pod associado")
  await runpod.startPod(m.runpod_pod_id)
  await db.from("machines").update({ status: "running" }).eq("id", machineId)
  await logEvent(machineId, "started", `Máquina "${m.name}" iniciada`)
  revalidatePath(`/machines/${machineId}`)
  revalidatePath("/machines")
}

export async function terminateMachine(machineId: string) {
  const db = createSupabaseAdmin()
  const { data: m } = await db.from("machines").select("*").eq("id", machineId).single<Machine>()
  if (m?.runpod_pod_id) {
    try {
      await runpod.deletePod(m.runpod_pod_id)
    } catch (e) {
      if (!String(e).includes("404")) throw e
    }
  }
  await db.from("machines").update({ status: "terminated" }).eq("id", machineId)
  await logEvent(machineId, "terminated", `Máquina "${m?.name}" apagada`)
  revalidatePath("/machines")
  redirect("/machines")
}

// ---------- Contas e chaves ----------

// Cria uma stack (produto/LLM contratado). Se não existir conta com o
// e-mail, cria a conta junto — o plan da conta nova é o plano da primeira
// stack; accounts.plan segue alimentando o roteamento e não muda para
// contas existentes. A stack nasce hospedada numa máquina: ou na escolhida
// Nome padrão das máquinas de stack: llm-stack-1, llm-stack-2, …
// Continua do maior sufixo existente (contando terminadas) para nunca
// reaproveitar nome.
async function nextStackMachineName(
  db: ReturnType<typeof createSupabaseAdmin>
): Promise<string> {
  const { data } = await db
    .from("machines")
    .select("name")
    .like("name", "llm-stack-%")
  let max = 0
  for (const row of (data ?? []) as { name: string }[]) {
    const m = /^llm-stack-(\d+)$/.exec(row.name)
    if (m) max = Math.max(max, Number(m[1]))
  }
  return `llm-stack-${max + 1}`
}

// GPUs do template que comportam o max_users configurado — validação feita
// ANTES de provisionar, para não deixar stack órfã quando nenhuma GPU serve.
async function viableGpuIdsForTemplate(
  tpl: Pick<
    Template,
    "gpu_types" | "gpu_count" | "max_users" | "model_footprint_gb" | "kv_reserve_gb_per_user"
  >
): Promise<string[]> {
  if (!tpl.gpu_types?.length) {
    throw new Error("O produto não tem tipos de GPU configurados")
  }
  const gpus = await listGpuTypes()
  const gpuCount = tpl.gpu_count ?? 1
  const rejections: string[] = []
  const viable = tpl.gpu_types.filter((gpuTypeId) => {
    const gpu = gpus.find((g) => g.id === gpuTypeId)
    const totalVramGb = gpu?.memoryInGb != null ? gpu.memoryInGb * gpuCount : null
    // VRAM desconhecida ou sem teto de usuários: não dá para validar — aceita.
    if (tpl.max_users === null || totalVramGb == null) return true
    const cap = vramSlots({
      vramGb: totalVramGb,
      modelFootprintGb: tpl.model_footprint_gb,
      kvReserveGbPerUser: tpl.kv_reserve_gb_per_user,
    })
    if (tpl.max_users > cap) {
      rejections.push(
        `${gpu?.displayName ?? gpuTypeId}: ${cap} vaga(s) — ${totalVramGb}GB VRAM − ${tpl.model_footprint_gb}GB modelo, ${tpl.kv_reserve_gb_per_user}GB/usuário`
      )
      return false
    }
    return true
  })
  if (viable.length === 0) {
    throw new Error(
      `Nenhuma GPU do produto comporta ${tpl.max_users} usuário(s): ${rejections.join("; ")}. ` +
        "Ajuste o max_users ou os valores de VRAM do produto."
    )
  }
  return viable
}

// pelo admin (machine_id do form), ou numa recém-provisionada com o
// template selecionado (nome = llm-stack-N, GPU = primeira compatível). Em
// ambos os casos emite a chave HEX da conta na máquina; a plainKey é
// retornada UMA única vez.
export async function createStack(formData: FormData): Promise<{
  slug: string
  machineId: string
  machineCreated: boolean
  plainKey: string
}> {
  const db = createSupabaseAdmin()
  const name = String(formData.get("name") || "").trim()
  const email = String(formData.get("email") || "").trim().toLowerCase()
  const plan = parsePlan(formData)
  const purchaseDate = String(formData.get("purchase_date") || "")
  const templateId = String(formData.get("template_id") || "")
  const chosenMachineId = String(formData.get("machine_id") || "")
  let slug = String(formData.get("slug") || "").trim()

  if (!name) throw new Error("Informe o nome do cliente")
  if (!email) throw new Error("Informe o e-mail do cliente")
  if (!templateId) throw new Error("Selecione um produto")
  if (!STACK_SLUG_RE.test(slug)) slug = generateStackSlug()

  const { data: tpl } = await db
    .from("templates")
    .select("id, plan, gpu_types, gpu_count, max_users, model_footprint_gb, kv_reserve_gb_per_user")
    .eq("id", templateId)
    .single<
      Pick<
        Template,
        "id" | "plan" | "gpu_types" | "gpu_count" | "max_users" | "model_footprint_gb" | "kv_reserve_gb_per_user"
      >
    >()
  if (!tpl) throw new Error("Produto não encontrado")

  // Sem máquina escolhida, uma será provisionada — valida ANTES de criar a
  // stack quais GPUs do template comportam o max_users configurado.
  let viableGpuIds: string[] = []
  if (!chosenMachineId) {
    viableGpuIds = await viableGpuIdsForTemplate(tpl)
  }

  // Máquina escolhida pelo admin: precisa estar rodando e ser do mesmo
  // template. A capacidade é validada adiante por createKey.
  if (chosenMachineId) {
    const { data: m } = await db
      .from("machines")
      .select("id, status, template_id")
      .eq("id", chosenMachineId)
      .single<Pick<Machine, "id" | "status" | "template_id">>()
    if (!m) throw new Error("Máquina não encontrada")
    if (m.status !== "running") throw new Error("A máquina escolhida não está rodando")
    if (m.template_id !== templateId) {
      throw new Error("A máquina escolhida não usa o produto selecionado")
    }
  }

  // Conta existente por e-mail (case-insensitive); senão cria.
  const { data: existing } = await db
    .from("accounts")
    .select("id")
    .ilike("email", email)
    .limit(1)
    .maybeSingle<{ id: string }>()

  let accountId = existing?.id
  if (!accountId) {
    const { data: created, error } = await db
      .from("accounts")
      .insert({ name, email, plan })
      .select("id")
      .single<{ id: string }>()
    if (error) throw new Error(error.message)
    accountId = created.id
  }

  // Insert com retry: colisão do unique de slug (Postgres 23505) regenera.
  let stackId: string | null = null
  for (let attempt = 0; attempt < 5; attempt++) {
    const { data: stack, error } = await db
      .from("stacks")
      .insert({
        account_id: accountId,
        plan,
        slug,
        ...(purchaseDate ? { purchase_date: purchaseDate } : {}),
      })
      .select("id")
      .single<{ id: string }>()
    if (!error && stack) {
      stackId = stack.id
      break
    }
    if (error && error.code !== "23505") throw new Error(error.message)
    slug = generateStackSlug()
  }
  if (!stackId) throw new Error("Não foi possível gerar um ID único; tente novamente")

  const machineCreated = !chosenMachineId
  let machineId = chosenMachineId
  if (!machineId) {
    // Tenta as GPUs viáveis em ordem — cobre falta de estoque de um tipo.
    let lastError = ""
    for (const gpuTypeId of viableGpuIds) {
      const prov = await provisionMachine({
        name: await nextStackMachineName(db),
        templateId,
        gpuTypeId,
      })
      if (!("error" in prov)) {
        machineId = prov.machineId
        break
      }
      lastError = prov.error
    }
    if (!machineId) {
      throw new Error(`Stack ${slug} criada, mas falhou ao criar a máquina: ${lastError}`)
    }
  }

  const { error: linkError } = await db
    .from("stacks")
    .update({ machine_id: machineId })
    .eq("id", stackId)
  if (linkError) throw new Error(linkError.message)

  const { plainKey } = await createKey({ accountId, machineId })

  revalidatePath("/contas")
  revalidatePath("/accounts")
  if (machineCreated) revalidatePath("/machines")
  return { slug, machineId, machineCreated, plainKey }
}

// Remove uma stack do painel. A máquina que a hospeda (se houver) não é
// afetada — descomissionar máquina é uma ação separada em /machines.
export async function deleteStack(id: string) {
  const db = createSupabaseAdmin()
  const { error } = await db.from("stacks").delete().eq("id", id)
  if (error) throw new Error(error.message)
  revalidatePath("/contas")
  revalidatePath("/accounts")
}

// Migra uma stack para outra máquina: garante a chave da conta no destino
// (reutiliza uma ativa ou emite nova — a plainKey é retornada UMA única
// vez), reaponta stacks.machine_id e revoga as chaves da conta na origem
// quando nenhuma outra stack dela continua lá. targetMachineId null =
// provisiona uma máquina nova (nome llm-stack-N) com o produto da stack.
export async function migrateStack(input: {
  stackId: string
  targetMachineId: string | null
}): Promise<{ machineId: string; machineCreated: boolean; plainKey: string | null }> {
  const db = createSupabaseAdmin()

  const { data: stack } = await db
    .from("stacks")
    .select("id, account_id, machine_id, plan, slug")
    .eq("id", input.stackId)
    .single<Pick<Stack, "id" | "account_id" | "machine_id" | "plan" | "slug">>()
  if (!stack) throw new Error("Stack não encontrada")

  const fromMachineId = stack.machine_id
  if (input.targetMachineId && input.targetMachineId === fromMachineId) {
    throw new Error("A stack já está nessa máquina")
  }

  // Template de referência: o da máquina atual; sem máquina, o produto
  // cadastrado com o plano da stack (mesma regra do createStack).
  let templateId: string | null = null
  if (fromMachineId) {
    const { data: m } = await db
      .from("machines")
      .select("template_id")
      .eq("id", fromMachineId)
      .single<Pick<Machine, "template_id">>()
    templateId = m?.template_id ?? null
  }
  if (!templateId) {
    const { data: tpl } = await db
      .from("templates")
      .select("id")
      .eq("plan", stack.plan)
      .limit(1)
      .maybeSingle<{ id: string }>()
    templateId = tpl?.id ?? null
  }
  if (!templateId) throw new Error(`Nenhum produto ${stack.plan} cadastrado`)

  const machineCreated = !input.targetMachineId
  let targetMachineId = input.targetMachineId
  if (targetMachineId) {
    const { data: target } = await db
      .from("machines")
      .select("id, status, template_id")
      .eq("id", targetMachineId)
      .single<Pick<Machine, "id" | "status" | "template_id">>()
    if (!target) throw new Error("Máquina de destino não encontrada")
    if (target.status !== "running") {
      throw new Error("A máquina de destino não está rodando")
    }
    if (target.template_id !== templateId) {
      throw new Error("A máquina de destino não usa o mesmo produto da stack")
    }
  } else {
    const { data: tpl } = await db
      .from("templates")
      .select("id, gpu_types, gpu_count, max_users, model_footprint_gb, kv_reserve_gb_per_user")
      .eq("id", templateId)
      .single<
        Pick<
          Template,
          "id" | "gpu_types" | "gpu_count" | "max_users" | "model_footprint_gb" | "kv_reserve_gb_per_user"
        >
      >()
    if (!tpl) throw new Error("Produto não encontrado")
    const viableGpuIds = await viableGpuIdsForTemplate(tpl)
    // Tenta as GPUs viáveis em ordem — cobre falta de estoque de um tipo.
    let lastError = ""
    for (const gpuTypeId of viableGpuIds) {
      const prov = await provisionMachine({
        name: await nextStackMachineName(db),
        templateId,
        gpuTypeId,
      })
      if (!("error" in prov)) {
        targetMachineId = prov.machineId
        break
      }
      lastError = prov.error
    }
    if (!targetMachineId) {
      throw new Error(`Falhou ao criar a máquina de destino: ${lastError}`)
    }
  }

  // Chave no destino ANTES de reapontar a stack — se a emissão falhar
  // (máquina lotada), a migração é abortada sem efeito colateral.
  const { data: existingKey } = await db
    .from("api_keys")
    .select("id")
    .eq("account_id", stack.account_id)
    .eq("machine_id", targetMachineId)
    .eq("status", "active")
    .limit(1)
    .maybeSingle<{ id: string }>()

  let plainKey: string | null = null
  if (!existingKey) {
    const created = await createKey({
      accountId: stack.account_id,
      machineId: targetMachineId,
    })
    plainKey = created.plainKey
  }

  const { error } = await db
    .from("stacks")
    .update({ machine_id: targetMachineId })
    .eq("id", stack.id)
  if (error) throw new Error(error.message)

  await logEvent(
    targetMachineId,
    "stack_migrated",
    `Stack ${stack.slug} migrada para esta máquina`
  )

  // Origem: revoga as chaves da conta quando nenhuma outra stack dela
  // continua na máquina antiga.
  if (fromMachineId) {
    const { count: remaining } = await db
      .from("stacks")
      .select("id", { count: "exact", head: true })
      .eq("account_id", stack.account_id)
      .eq("machine_id", fromMachineId)
    if (!remaining) {
      const { data: oldKeys } = await db
        .from("api_keys")
        .select("id")
        .eq("account_id", stack.account_id)
        .eq("machine_id", fromMachineId)
        .eq("status", "active")
      for (const k of (oldKeys ?? []) as { id: string }[]) {
        await revokeKey(k.id)
      }
    }
  }

  revalidatePath("/contas")
  revalidatePath("/accounts")
  if (fromMachineId) revalidatePath(`/machines/${fromMachineId}`)
  revalidatePath(`/machines/${targetMachineId}`)
  if (machineCreated) revalidatePath("/machines")
  return { machineId: targetMachineId, machineCreated, plainKey }
}

// Atualiza plano e/ou system prompt de uma conta já existente.
export async function updateAccountConfig(formData: FormData) {
  const db = createSupabaseAdmin()
  const accountId = String(formData.get("account_id"))
  if (!accountId) throw new Error("Conta não informada")

  const plan = parsePlan(formData)
  const systemPrompt = String(formData.get("system_prompt") || "").trim() || null

  const { error } = await db
    .from("accounts")
    .update({ plan, system_prompt: systemPrompt })
    .eq("id", accountId)
  if (error) throw new Error(error.message)
  revalidatePath("/contas")
  revalidatePath("/accounts")
}

// Gera uma chave HEX para uma conta numa máquina.
// Retorna a chave em texto puro UMA única vez.
export async function createKey(input: {
  accountId: string
  machineId: string
}): Promise<{ plainKey: string }> {
  const db = createSupabaseAdmin()

  // Valida o limite de slots da máquina antes de emitir a chave.
  // Check-then-insert: há corrida teórica entre duas emissões simultâneas,
  // aceitável para um painel de administração.
  const { data: m } = await db
    .from("machines")
    .select("*")
    .eq("id", input.machineId)
    .single<Machine>()
  if (!m) throw new Error("Máquina não encontrada")

  const { count: activeKeys } = await db
    .from("api_keys")
    .select("id", { count: "exact", head: true })
    .eq("machine_id", input.machineId)
    .eq("status", "active")

  const { data: tpl } = m.template_id
    ? await db.from("templates").select("*").eq("id", m.template_id).single<Template>()
    : { data: null }

  const cap = computeCapacity({
    vramGb: m.vram_gb,
    modelFootprintGb: tpl?.model_footprint_gb ?? 16,
    kvReserveGbPerUser: tpl?.kv_reserve_gb_per_user ?? 2,
    activeKeys: activeKeys ?? 0,
    maxUsers: m.max_users,
  })
  // slotsMax = 0 significa capacidade desconhecida (sem VRAM nem teto) — não bloqueia
  if (cap.slotsMax > 0 && cap.slotsUsed >= cap.slotsMax) {
    throw new Error(`Limite de ${cap.slotsMax} usuário(s) atingido nesta máquina`)
  }

  const plainKey = generateHexKey()

  const { error } = await db.from("api_keys").insert({
    account_id: input.accountId,
    machine_id: input.machineId,
    key_hash: hashKey(plainKey),
    key_prefix: keyPrefix(plainKey),
    plain_key: plainKey,
    status: "active",
  })
  if (error) throw new Error(error.message)

  await logEvent(input.machineId, "key_created", `Nova chave criada (${keyPrefix(plainKey)}…)`)
  await syncMachineKeys(input.machineId).catch((e) =>
    console.error("Sync com agent falhou (a chave foi salva):", e)
  )
  revalidatePath("/accounts")
  revalidatePath(`/machines/${input.machineId}`)
  return { plainKey }
}

// Invalida o cache de chaves do gateway (best-effort) — sem isso, uma chave
// revogada continuaria válida no gateway até o TTL do cache expirar.
async function flushGatewayKeyCache() {
  const url = process.env.GATEWAY_URL
  const secret = process.env.GATEWAY_ADMIN_SECRET
  if (!url || !secret) return // gateway ainda não configurado
  await fetch(`${url.replace(/\/$/, "")}/admin/flush-key-cache`, {
    method: "POST",
    headers: { "X-Admin-Secret": secret },
    signal: AbortSignal.timeout(5_000),
  })
}

export async function revokeKey(keyId: string) {
  const db = createSupabaseAdmin()
  const { data: key } = await db
    .from("api_keys")
    .update({ status: "revoked" })
    .eq("id", keyId)
    .select()
    .single<ApiKey>()
  if (key) {
    await logEvent(key.machine_id, "key_revoked", `Chave ${key.key_prefix}… revogada`)
    await syncMachineKeys(key.machine_id).catch((e) =>
      console.error("Sync com agent falhou (a chave foi revogada no banco):", e)
    )
    await flushGatewayKeyCache().catch((e) =>
      console.error("Flush do cache do gateway falhou (a chave foi revogada no banco):", e)
    )
    revalidatePath("/accounts")
    revalidatePath(`/machines/${key.machine_id}`)
  }
}

// ---------- Adapters LoRA ----------

// Bucket privado no Supabase Storage onde os adapters ficam armazenados.
// Convenção de path dentro do bucket: {account_id}/{version}/adapter_*.safetensors
const LORA_BUCKET = "loras"

// Arquivos obrigatórios de um adapter no formato PEFT.
const LORA_REQUIRED_FILES = ["adapter_config.json", "adapter_model.safetensors"]

// Whitelist completa aceita pelo agent (/admin/load-lora) — manter em sincronia
// com LORA_ALLOWED_FILES em docker/agent/main.py.
const LORA_ALLOWED_FILES = new Set([
  ...LORA_REQUIRED_FILES,
  "tokenizer.json",
  "tokenizer_config.json",
  "special_tokens_map.json",
  "added_tokens.json",
  "chat_template.jinja",
])

// Registra um adapter LoRA já existente no bucket (o treino acontece fora
// deste sistema). Valida que o prefixo contém os arquivos do formato PEFT
// antes de registrar — melhor falhar aqui do que na hora de servir o cliente.
export async function registerLoraAdapter(formData: FormData) {
  const db = createSupabaseAdmin()
  const accountId = String(formData.get("account_id"))
  const version = String(formData.get("version") || "").trim()
  if (!accountId) throw new Error("Conta não informada")
  if (!version) throw new Error("Versão não informada")

  const storagePath = `${accountId}/${version}`
  const { data: files, error: listErr } = await db.storage
    .from(LORA_BUCKET)
    .list(storagePath)
  if (listErr) throw new Error(`Falha ao acessar o bucket "${LORA_BUCKET}": ${listErr.message}`)

  // entradas sem id são "pastas" — uma pasta chamada adapter_config.json não
  // conta como arquivo (o dashboard do Supabase facilita esse engano)
  const names = new Set((files ?? []).filter((f) => f.id).map((f) => f.name))
  const missing = LORA_REQUIRED_FILES.filter((f) => !names.has(f))
  if (missing.length > 0) {
    throw new Error(
      `Adapter incompleto em ${LORA_BUCKET}/${storagePath} — faltam: ${missing.join(", ")} (formato PEFT esperado; use scripts/upload-lora.mjs)`
    )
  }

  const { error } = await db.from("lora_adapters").insert({
    account_id: accountId,
    storage_path: storagePath,
    version,
    status: "ready",
  })
  if (error) throw new Error(error.message)
  revalidatePath("/accounts")
}

export async function listLoraAdapters(accountId?: string): Promise<LoraAdapter[]> {
  const db = createSupabaseAdmin()
  let query = db
    .from("lora_adapters")
    .select("*")
    .order("created_at", { ascending: false })
  if (accountId) query = query.eq("account_id", accountId)
  const { data, error } = await query
  if (error) throw new Error(error.message)
  return (data ?? []) as LoraAdapter[]
}

// Nome canônico do adapter dentro do vLLM: determinístico por conta, usado
// pelo gateway para reescrever o campo "model" da request.
function loraName(accountId: string): string {
  return `acct-${accountId}`
}

// Gera signed URLs (TTL curto) para os arquivos do adapter no bucket —
// o pod baixa direto do storage sem precisar de credenciais Supabase.
async function buildLoraSignedFiles(adapter: LoraAdapter): Promise<LoraSignedFile[]> {
  const db = createSupabaseAdmin()
  const { data: files, error: listErr } = await db.storage
    .from(LORA_BUCKET)
    .list(adapter.storage_path)
  if (listErr) throw new Error(`Falha ao listar o adapter no bucket: ${listErr.message}`)
  if (!files || files.length === 0) {
    throw new Error(`Adapter sem arquivos em ${LORA_BUCKET}/${adapter.storage_path}`)
  }

  const signed: LoraSignedFile[] = []
  // só assina arquivos da whitelist PEFT — o agent rejeita qualquer outro
  const wanted = files.filter((f) => LORA_ALLOWED_FILES.has(f.name))
  for (const f of wanted) {
    const { data, error } = await db.storage
      .from(LORA_BUCKET)
      .createSignedUrl(`${adapter.storage_path}/${f.name}`, 600)
    if (error || !data) {
      throw new Error(`Falha ao assinar URL de ${f.name}: ${error?.message}`)
    }
    signed.push({ name: f.name, url: data.signedUrl })
  }
  return signed
}

// Carrega o adapter de uma conta numa máquina específica (teste manual via
// painel/script; o fluxo automático chega com o gateway).
export async function loadLoraOnMachine(machineId: string, adapterId: string) {
  const db = createSupabaseAdmin()
  const { data: m } = await db.from("machines").select("*").eq("id", machineId).single<Machine>()
  if (!m) throw new Error("Máquina não encontrada")
  const { data: adapter } = await db
    .from("lora_adapters")
    .select("*")
    .eq("id", adapterId)
    .single<LoraAdapter>()
  if (!adapter) throw new Error("Adapter não encontrado")
  if (adapter.status !== "ready") throw new Error("Adapter marcado como inválido")

  const files = await buildLoraSignedFiles(adapter)
  const result = await agent.loadLora(m, { lora_name: loraName(adapter.account_id), files })
  await logEvent(
    machineId,
    "sync",
    `Adapter ${loraName(adapter.account_id)} carregado (download ${result.download_s}s, load ${result.load_s}s)`
  )
  revalidatePath(`/machines/${machineId}`)
  return result
}

export async function unloadLoraOnMachine(machineId: string, accountId: string) {
  const db = createSupabaseAdmin()
  const { data: m } = await db.from("machines").select("*").eq("id", machineId).single<Machine>()
  if (!m) throw new Error("Máquina não encontrada")
  const result = await agent.unloadLora(m, loraName(accountId))
  await logEvent(machineId, "sync", `Adapter ${loraName(accountId)} descarregado`)
  revalidatePath(`/machines/${machineId}`)
  return result
}

// Push da lista de chaves ativas para o agent da máquina
export async function syncMachineKeys(machineId: string) {
  const db = createSupabaseAdmin()
  const { data: m } = await db.from("machines").select("*").eq("id", machineId).single<Machine>()
  if (!m) throw new Error("Máquina não encontrada")

  const { data: keys } = await db
    .from("api_keys")
    .select("key_hash, key_prefix, status, accounts(name)")
    .eq("machine_id", machineId)
    .eq("status", "active")

  const entries: AgentKeyEntry[] = (keys ?? []).map((k) => ({
    key_hash: k.key_hash,
    key_prefix: k.key_prefix,
    account_name:
      (k.accounts as unknown as { name: string } | null)?.name ?? "desconhecida",
  }))

  await agent.syncKeys(m, entries)
  await logEvent(machineId, "sync", `${entries.length} chave(s) sincronizada(s) com o agent`)
}

// ---------- Base de conhecimento (RAG) ----------

// Bucket privado no Supabase Storage onde os arquivos crus da base de
// conhecimento ficam armazenados. Convenção de path: {account_id}/{filename}
const KNOWLEDGE_BUCKET = "knowledge"

// Modelo de embedding da OpenAI usado tanto aqui (indexação) quanto no
// gateway (embed da query) — precisam ser o mesmo modelo/dimensão (1536).
const EMBEDDING_MODEL = "text-embedding-3-small"

// Chunking fixo por tamanho de caractere com overlap — suficiente para o
// RAG básico do VibeCoder; nada de chunking semântico/estrutural por ora.
const CHUNK_SIZE = 1000
const CHUNK_OVERLAP = 100

function chunkText(text: string): string[] {
  const chunks: string[] = []
  let start = 0
  while (start < text.length) {
    const end = Math.min(start + CHUNK_SIZE, text.length)
    const chunk = text.slice(start, end).trim()
    if (chunk) chunks.push(chunk)
    if (end === text.length) break
    start = end - CHUNK_OVERLAP
  }
  return chunks
}

async function embedTexts(texts: string[]): Promise<number[][]> {
  const apiKey = process.env.OPENAI_API_KEY
  if (!apiKey) throw new Error("OPENAI_API_KEY não configurada")

  const resp = await fetch("https://api.openai.com/v1/embeddings", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ model: EMBEDDING_MODEL, input: texts }),
  })
  if (!resp.ok) {
    throw new Error(`Falha ao gerar embeddings: ${resp.status} ${await resp.text()}`)
  }
  const data = (await resp.json()) as { data: { embedding: number[] }[] }
  return data.data.map((d) => d.embedding)
}

// Sobe um arquivo de texto pro bucket de conhecimento, chunka e indexa os
// embeddings — só texto puro/Markdown (sem parsing de PDF/DOCX nesta rodada).
export async function uploadKnowledgeFile(formData: FormData) {
  const db = createSupabaseAdmin()
  const accountId = String(formData.get("account_id"))
  const file = formData.get("file")
  if (!accountId) throw new Error("Conta não informada")
  if (!(file instanceof File) || file.size === 0) throw new Error("Arquivo não informado")
  if (!/\.(txt|md)$/i.test(file.name)) {
    throw new Error("Só arquivos .txt ou .md são aceitos por enquanto")
  }

  const text = await file.text()
  const chunks = chunkText(text)
  if (chunks.length === 0) throw new Error("Arquivo vazio")

  const storagePath = `${accountId}/${file.name}`
  const { error: uploadErr } = await db.storage
    .from(KNOWLEDGE_BUCKET)
    .upload(storagePath, file, { upsert: true, contentType: "text/plain" })
  if (uploadErr) throw new Error(`Falha ao subir arquivo: ${uploadErr.message}`)

  const embeddings = await embedTexts(chunks)

  // substitui qualquer indexação anterior deste mesmo arquivo
  await db.from("knowledge_chunks").delete().eq("storage_path", storagePath)
  const { error: insertErr } = await db.from("knowledge_chunks").insert(
    chunks.map((content, i) => ({
      account_id: accountId,
      storage_path: storagePath,
      chunk_index: i,
      content,
      embedding: embeddings[i],
    }))
  )
  if (insertErr) throw new Error(`Falha ao indexar chunks: ${insertErr.message}`)

  revalidatePath("/contas")
}

export async function listKnowledgeFiles(
  accountId: string
): Promise<{ storage_path: string; chunks: number }[]> {
  const db = createSupabaseAdmin()
  const { data, error } = await db
    .from("knowledge_chunks")
    .select("storage_path")
    .eq("account_id", accountId)
  if (error) throw new Error(error.message)

  const counts = new Map<string, number>()
  for (const row of data ?? []) {
    counts.set(row.storage_path, (counts.get(row.storage_path) ?? 0) + 1)
  }
  return Array.from(counts, ([storage_path, chunks]) => ({ storage_path, chunks }))
}

export async function deleteKnowledgeFile(accountId: string, storagePath: string) {
  const db = createSupabaseAdmin()
  await db.storage.from(KNOWLEDGE_BUCKET).remove([storagePath])
  const { error } = await db
    .from("knowledge_chunks")
    .delete()
    .eq("account_id", accountId)
    .eq("storage_path", storagePath)
  if (error) throw new Error(error.message)
  revalidatePath("/contas")
}

// ---------- Migração de conta ----------

// Move o adapter LoRA de uma conta da máquina atual para outra: descarrega
// na origem, carrega no destino, atualiza routing_state e grava o evento em
// routing_history. Ação manual via painel — o fluxo automático (rebalance)
// não existe ainda.
export async function migrateAccountToMachine(accountId: string, targetMachineId: string) {
  const db = createSupabaseAdmin()

  const current = await getClientLocation(accountId)
  const { data: adapter } = await db
    .from("lora_adapters")
    .select("*")
    .eq("account_id", accountId)
    .eq("status", "ready")
    .order("created_at", { ascending: false })
    .limit(1)
    .maybeSingle<LoraAdapter>()
  if (!adapter) throw new Error("Conta não tem adapter LoRA pronto para migrar")

  const fromMachineId = current?.machine_id ?? null
  if (fromMachineId === targetMachineId) {
    throw new Error("A conta já está nessa máquina")
  }

  if (fromMachineId) {
    await unloadLoraOnMachine(fromMachineId, accountId)
  }
  await loadLoraOnMachine(targetMachineId, adapter.id)

  await setClientLocation(accountId, {
    machine_id: targetMachineId,
    lora_adapter_id: adapter.id,
    lora_status: "loaded",
  })
  await db.from("routing_history").insert({
    account_id: accountId,
    event: "migrated",
    machine_id: targetMachineId,
    from_machine_id: fromMachineId,
    lora_adapter_id: adapter.id,
  })

  revalidatePath("/contas")
  if (fromMachineId) revalidatePath(`/machines/${fromMachineId}`)
  revalidatePath(`/machines/${targetMachineId}`)
}

