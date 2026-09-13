// Promove para o template Pro de PRODUÇÃO os args validados no
// PRO-2xA40-AGENTIC-TEST (13/09/2026): MTP off, --max-num-batched-tokens 16384,
// --language-model-only, --default-chat-template-kwargs reasoning_effort=low.
//
// Só VLLM_EXTRA_ARGS muda; deriva dos args atuais (mesma função do script de
// criação do template de teste) em vez de reescrever a string. Grava no RunPod
// (PATCH /templates/{id}, mesmo caminho de lib/runpod.ts:updateTemplate) e no
// Supabase, com verify de leitura de volta nos dois — precedente do
// updateTemplate do painel que engole erro em silêncio.
//
// Os pods que já estão de pé NÃO mudam: o vLLM lê as flags só no boot. Recriar
// as máquinas do Pro depois é o passo que de fato coloca isso em produção.
//
// Guarda os args anteriores em scripts/.pro-args-prev.json para o rollback.
//
//   node --env-file=.env scripts/_tmp-promote-agentic-args.mjs            # aplica
//   node --env-file=.env scripts/_tmp-promote-agentic-args.mjs --rollback # desfaz

import { readFile, writeFile, unlink } from "node:fs/promises"
import { createClient } from "@supabase/supabase-js"

const PROD_TEMPLATE = "PRO-2xA40"
const ADDED_FLAGS = [
  "--max-num-batched-tokens 16384",
  "--language-model-only",
  '--default-chat-template-kwargs {"reasoning_effort":"low"}',
]
const PREV_FILE = new URL("./.pro-args-prev.json", import.meta.url)
const RUNPOD_REST = "https://rest.runpod.io/v1"

const db = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL,
  process.env.SUPABASE_SERVICE_ROLE_KEY,
  { auth: { persistSession: false } },
)

async function runpod(path, init = {}) {
  const { json, ...rest } = init
  const res = await fetch(`${RUNPOD_REST}${path}`, {
    ...rest,
    headers: {
      Authorization: `Bearer ${process.env.RUNPOD_API_KEY}`,
      ...(json !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    body: json !== undefined ? JSON.stringify(json) : undefined,
  })
  if (!res.ok) throw new Error(`RunPod ${rest.method ?? "GET"} ${path} → ${res.status}: ${await res.text()}`)
  return res.status === 204 ? undefined : res.json()
}

function deriveVllmArgs(sourceArgs) {
  const tokens = sourceArgs.trim().split(/\s+/)
  const out = []
  for (let i = 0; i < tokens.length; i++) {
    if (tokens[i] === "--speculative-config") { i += 1; continue }
    out.push(tokens[i])
  }
  for (const flag of ADDED_FLAGS) {
    if (out.includes(flag.split(" ")[0])) throw new Error(`já tem ${flag.split(" ")[0]} — template não está no baseline esperado`)
  }
  const served = out.indexOf("--served-model-name")
  out.splice(served === -1 ? out.length : served, 0, ...ADDED_FLAGS)
  return out.join(" ")
}

async function applyArgs(tpl, args, label) {
  const env = { ...tpl.env, VLLM_EXTRA_ARGS: args }
  if (tpl.runpod_template_id) {
    await runpod(`/templates/${tpl.runpod_template_id}`, { method: "PATCH", json: { env } })
    const remote = await runpod(`/templates/${tpl.runpod_template_id}`)
    const remoteArgs = remote?.env?.VLLM_EXTRA_ARGS ?? (Array.isArray(remote?.env) ? remote.env.find((e) => e.key === "VLLM_EXTRA_ARGS")?.value : undefined)
    console.log(`  runpod ${label}: ${remoteArgs === args ? "ok" : "FALHA (leitura de volta divergiu)"}`)
    if (remoteArgs !== args) console.log("   remoto:", remoteArgs)
  } else {
    console.log("  (template sem runpod_template_id — só Supabase)")
  }
  const { error } = await db.from("templates").update({ env }).eq("id", tpl.id)
  if (error) throw new Error(`supabase update: ${error.message}`)
  const { data: back } = await db.from("templates").select("env").eq("id", tpl.id).single()
  console.log(`  supabase ${label}: ${back.env.VLLM_EXTRA_ARGS === args ? "ok" : "FALHA (leitura de volta divergiu)"}`)
}

const { data: tpl, error } = await db.from("templates").select("*").eq("name", PROD_TEMPLATE).single()
if (error) throw new Error(`template ${PROD_TEMPLATE}: ${error.message}`)
console.log(`template: ${tpl.name} (${tpl.id}) runpod=${tpl.runpod_template_id}`)
console.log("args atuais:", tpl.env.VLLM_EXTRA_ARGS)

if (process.argv.includes("--rollback")) {
  let prev
  try { prev = JSON.parse(await readFile(PREV_FILE, "utf8")) } catch { throw new Error("sem .pro-args-prev.json — nada a desfazer") }
  if (prev.template_id !== tpl.id) throw new Error("arquivo de rollback é de outro template")
  console.log("args de volta:", prev.previous_args)
  await applyArgs(tpl, prev.previous_args, "rollback")
  await unlink(PREV_FILE)
  console.log("\nok: rollback aplicado. Máquinas de pé só mudam ao serem recriadas.")
  process.exit(0)
}

let prevExists = false
try { await readFile(PREV_FILE); prevExists = true } catch {}
if (prevExists) throw new Error("já existe .pro-args-prev.json — promoção já aplicada; rode --rollback antes de repetir")

const novo = deriveVllmArgs(tpl.env.VLLM_EXTRA_ARGS)
console.log("args novos :", novo)
await writeFile(PREV_FILE, JSON.stringify({ template_id: tpl.id, previous_args: tpl.env.VLLM_EXTRA_ARGS, applied_at: new Date().toISOString() }, null, 2))
await applyArgs(tpl, novo, "promoção")
console.log("\nok: template de produção atualizado. Recriar as máquinas do Pro para valer nos pods.")
