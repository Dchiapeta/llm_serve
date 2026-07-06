"use server"

import { revalidatePath } from "next/cache"
import { redirect } from "next/navigation"
import { randomBytes } from "crypto"

import { agent, type AgentKeyEntry } from "./agent"
import { computeCapacity, vramSlots } from "./capacity"
import { generateHexKey, hashKey, keyPrefix } from "./keys"
import { listGpuTypes, podProxyUrl, runpod } from "./runpod"
import { createSupabaseAdmin, createSupabaseServerClient } from "./supabase/server"
import type { ApiKey, Machine, Template } from "./types"

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
  const diskGb = Number(formData.get("disk_gb") || 40)
  const footprint = Number(formData.get("model_footprint_gb") || 16)
  const kvReserve = Number(formData.get("kv_reserve_gb_per_user") || 2)
  const maxUsers = parseMaxUsers(formData)
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
    gpu_types: gpuTypes,
    env,
    start_command: startCommand,
    disk_gb: diskGb,
    volume_gb: volumeGb,
    volume_mount_path: volumeMountPath,
    http_ports: httpPorts,
    tcp_ports: tcpPorts,
    model_footprint_gb: footprint,
    kv_reserve_gb_per_user: kvReserve,
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
  const footprint = Number(formData.get("model_footprint_gb") || 16)
  const kvReserve = Number(formData.get("kv_reserve_gb_per_user") || 2)
  const maxUsers = parseMaxUsers(formData)
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
    gpu_types: gpuTypes,
    env: remote.env ?? {},
    start_command: (remote.dockerStartCmd ?? []).join(" ") || null,
    disk_gb: remote.containerDiskInGb ?? 40,
    volume_gb: remote.volumeInGb ?? 0,
    volume_mount_path: remote.volumeMountPath || "/workspace",
    http_ports: httpPorts,
    tcp_ports: tcpPorts,
    model_footprint_gb: footprint,
    kv_reserve_gb_per_user: kvReserve,
    max_users: maxUsers,
  })
  if (error) throw new Error(error.message)
  revalidatePath("/templates")
}

export async function updateTemplate(formData: FormData) {
  const db = createSupabaseAdmin()
  const id = String(formData.get("id"))
  if (!id) throw new Error("Template não informado")

  const name = String(formData.get("name"))
  const image = String(formData.get("image"))
  const modelName = String(formData.get("model_name"))
  const diskGb = Number(formData.get("disk_gb") || 40)
  const footprint = Number(formData.get("model_footprint_gb") || 16)
  const kvReserve = Number(formData.get("kv_reserve_gb_per_user") || 2)
  const maxUsers = parseMaxUsers(formData)
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
      gpu_types: gpuTypes,
      env,
      start_command: startCommand,
      disk_gb: diskGb,
      volume_gb: volumeGb,
      volume_mount_path: volumeMountPath,
      http_ports: httpPorts,
      tcp_ports: tcpPorts,
      model_footprint_gb: footprint,
      kv_reserve_gb_per_user: kvReserve,
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

export async function createMachine(formData: FormData) {
  const db = createSupabaseAdmin()
  const name = String(formData.get("name"))
  const templateId = String(formData.get("template_id"))
  const gpuTypeId = String(formData.get("gpu_type"))

  const { data: tpl, error: tplErr } = await db
    .from("templates")
    .select("*")
    .eq("id", templateId)
    .single<Template>()
  if (tplErr || !tpl) return { error: "Template não encontrado" }

  // teto manual: valor do form, com fallback para o padrão do template
  const maxUsers = parseMaxUsers(formData) ?? tpl.max_users

  const adminSecret = randomBytes(24).toString("hex")

  const gpus = await listGpuTypes()
  const gpu = gpus.find((g) => g.id === gpuTypeId)

  if (maxUsers !== null && gpu?.memoryInGb != null) {
    const cap = vramSlots({
      vramGb: gpu.memoryInGb,
      modelFootprintGb: tpl.model_footprint_gb,
      kvReserveGbPerUser: tpl.kv_reserve_gb_per_user,
    })
    if (maxUsers > cap) {
      return {
        error: `A GPU ${gpu.displayName} comporta no máximo ${cap} usuário(s) para este modelo (pedido: ${maxUsers})`,
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
      gpuCount: 1,
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
      gpu_type: gpu?.displayName ?? gpuTypeId,
      status: "creating",
      template_id: tpl.id,
      admin_secret: adminSecret,
      model_name: tpl.model_name,
      vram_gb: gpu?.memoryInGb ?? null,
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
  redirect(`/machines/${machine.id}`)
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

export async function createAccount(formData: FormData) {
  const db = createSupabaseAdmin()
  const { error } = await db.from("accounts").insert({
    name: String(formData.get("name")),
    email: String(formData.get("email") || "") || null,
  })
  if (error) throw new Error(error.message)
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
    revalidatePath("/accounts")
    revalidatePath(`/machines/${key.machine_id}`)
  }
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

