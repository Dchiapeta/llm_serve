// Roteia a stack de teste do Pro para a máquina do PRO-2xA40-AGENTIC-TEST
// (e desfaz depois).
//
// Mecanismo (memória rotear-trafego-template-is-test): fixar stacks.machine_id.
// resolve_base_machine no gateway honra o pin sem olhar is_test, então a chave
// da stack passa a bater na máquina nova sem mexer em template default nem em
// config do cliente (o alias servido é o mesmo pro-base).
//
// Depois de mudar a stack, invalida o cache de chaves do gateway
// (/admin/flush-key-cache, mesmo caminho de lib/actions.ts:flushGatewayKeyCache)
// e pede o sync das chaves para a máquina nova (/admin/sync-machine-keys), senão
// o gateway pode seguir resolvendo a máquina antiga até o TTL do cache expirar,
// e o agent do pod novo ainda não conhece a chave.
//
// A máquina anterior fica gravada em scripts/.agentic-route-prev.json para o
// rollback — que é OBRIGATÓRIO ao fim do teste: enquanto o pin existir, a stack
// não volta ao template default do plano.
//
//   node --env-file=.env scripts/_tmp-route-agentic-test.mjs status
//   node --env-file=.env scripts/_tmp-route-agentic-test.mjs pin <nome-ou-id-da-máquina>
//   node --env-file=.env scripts/_tmp-route-agentic-test.mjs rollback

import { readFile, writeFile, unlink } from "node:fs/promises"
import { createClient } from "@supabase/supabase-js"

const STACK_ID = "f06dac7c-217a-4f48-9496-81181f034e90" // conta-de-teste (Pro)
const TEMPLATE_ID = "75e1d916-7d83-4d77-9b95-89e349e025d2" // PRO-2xA40-AGENTIC-TEST
const PREV_FILE = new URL("./.agentic-route-prev.json", import.meta.url)

const db = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL,
  process.env.SUPABASE_SERVICE_ROLE_KEY,
  { auth: { persistSession: false } },
)

async function gatewayAdmin(path, body) {
  const url = process.env.GATEWAY_URL
  const secret = process.env.GATEWAY_ADMIN_SECRET
  if (!url || !secret) {
    console.warn(`  (GATEWAY_URL/GATEWAY_ADMIN_SECRET ausentes — pulando ${path}; o cache expira sozinho pelo TTL)`)
    return
  }
  const res = await fetch(`${url.replace(/\/$/, "")}${path}`, {
    method: "POST",
    headers: { "X-Admin-Secret": secret, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(10_000),
  })
  console.log(`  gateway ${path} → ${res.status}`)
}

async function loadStack() {
  const { data, error } = await db
    .from("stacks")
    .select("id, name, plan, machine_id")
    .eq("id", STACK_ID)
    .single()
  if (error) throw new Error(`stack: ${error.message}`)
  return data
}

async function loadMachine(idOrName) {
  const cols = "id, name, status, template_id, served_model_name, max_model_len, public_url"
  let q = db.from("machines").select(cols)
  q = /^[0-9a-f-]{36}$/i.test(idOrName) ? q.eq("id", idOrName) : q.eq("name", idOrName)
  const { data, error } = await q.maybeSingle()
  if (error) throw new Error(`machine: ${error.message}`)
  return data
}

async function describe(label, machineId) {
  if (!machineId) return console.log(`${label}: (sem máquina)`)
  const m = await loadMachine(machineId)
  if (!m) return console.log(`${label}: ${machineId} (não encontrada)`)
  const tag = m.template_id === TEMPLATE_ID ? " [AGENTIC-TEST]" : ""
  console.log(`${label}: ${m.name} (${m.id}) status=${m.status} alias=${m.served_model_name} janela=${m.max_model_len}${tag}`)
}

const [mode, arg] = process.argv.slice(2)
const stack = await loadStack()
console.log(`stack: ${stack.name} (${stack.id}) plano=${stack.plan}`)
await describe("máquina atual", stack.machine_id)

if (mode === "status" || !mode) {
  try {
    const prev = JSON.parse(await readFile(PREV_FILE, "utf8"))
    console.log(`pin ativo desde ${prev.pinned_at}; rollback devolve para:`)
    await describe("  máquina anterior", prev.previous_machine_id)
  } catch {
    console.log("nenhum pin ativo (sem .agentic-route-prev.json)")
  }
  process.exit(0)
}

if (mode === "pin") {
  if (!arg) throw new Error("uso: pin <nome-ou-id-da-máquina>")
  const target = await loadMachine(arg)
  if (!target) throw new Error(`máquina não encontrada: ${arg}`)
  if (target.template_id !== TEMPLATE_ID) {
    throw new Error(`máquina ${target.name} não é do template PRO-2xA40-AGENTIC-TEST (template_id=${target.template_id})`)
  }
  if (target.status !== "running") {
    throw new Error(`máquina ${target.name} está ${target.status}, não running — suba/espere o pod antes de rotear`)
  }
  if (stack.machine_id === target.id) {
    console.log("já apontada para essa máquina — nada a fazer")
    process.exit(0)
  }
  let prevExists = false
  try { await readFile(PREV_FILE); prevExists = true } catch {}
  if (prevExists) throw new Error("já existe um pin ativo — rode rollback antes de pinar de novo")

  await writeFile(PREV_FILE, JSON.stringify({
    stack_id: STACK_ID,
    previous_machine_id: stack.machine_id,
    pinned_to: target.id,
    pinned_at: new Date().toISOString(),
  }, null, 2))

  const { error } = await db.from("stacks").update({ machine_id: target.id }).eq("id", STACK_ID)
  if (error) throw new Error(`update falhou: ${error.message}`)

  await gatewayAdmin("/admin/flush-key-cache")
  await gatewayAdmin("/admin/sync-machine-keys", { machine_id: target.id })

  const after = await loadStack()
  console.log(after.machine_id === target.id ? "\nok: pin aplicado" : "\nFALHA: verify de leitura de volta divergiu")
  await describe("máquina agora", after.machine_id)
  console.log("\nlembrete: rollback OBRIGATÓRIO ao fim do teste.")
  process.exit(after.machine_id === target.id ? 0 : 1)
}

if (mode === "rollback") {
  let prev
  try { prev = JSON.parse(await readFile(PREV_FILE, "utf8")) } catch {
    throw new Error("sem pin ativo (não há .agentic-route-prev.json) — nada a desfazer")
  }
  if (prev.stack_id !== STACK_ID) throw new Error("arquivo de pin é de outra stack")

  const { error } = await db.from("stacks").update({ machine_id: prev.previous_machine_id }).eq("id", STACK_ID)
  if (error) throw new Error(`update falhou: ${error.message}`)

  await gatewayAdmin("/admin/flush-key-cache")
  if (prev.previous_machine_id) await gatewayAdmin("/admin/sync-machine-keys", { machine_id: prev.previous_machine_id })

  const after = await loadStack()
  const ok = after.machine_id === prev.previous_machine_id
  console.log(ok ? "\nok: rollback aplicado" : "\nFALHA: verify de leitura de volta divergiu")
  await describe("máquina agora", after.machine_id)
  if (ok) await unlink(PREV_FILE)
  process.exit(ok ? 0 : 1)
}

throw new Error(`modo desconhecido: ${mode} (use status | pin <máquina> | rollback)`)
