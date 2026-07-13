#!/usr/bin/env node
// Mede o tempo de load de um adapter LoRA: download do Supabase Storage até
// estar pronto para inferência no vLLM. O número obtido aqui orienta a
// estratégia de migração entre máquinas (Fase 5).
//
// Uso:
//   node scripts/test-lora-load.mjs \
//     --agent-url https://<pod-id>-8000.proxy.runpod.net \
//     --admin-secret <AGENT_ADMIN_SECRET> \
//     --account-id <uuid da conta> \
//     --version v1 \
//     [--api-key <chave HEX para o teste de inferência>] \
//     [--model-base <nome do modelo base>]
//
// Requer no ambiente (mesmos do painel, ex: source .env.local):
//   NEXT_PUBLIC_SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

import { createClient } from "@supabase/supabase-js"

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`)
  if (i === -1 || !process.argv[i + 1]) {
    if (fallback !== undefined) return fallback
    console.error(`Faltou --${name}`)
    process.exit(1)
  }
  return process.argv[i + 1]
}

const AGENT_URL = arg("agent-url").replace(/\/$/, "")
const ADMIN_SECRET = arg("admin-secret")
const ACCOUNT_ID = arg("account-id")
const VERSION = arg("version", "v1")
const API_KEY = arg("api-key", "")
const MODEL_BASE = arg("model-base", "")

const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL
const SERVICE_ROLE = process.env.SUPABASE_SERVICE_ROLE_KEY
if (!SUPABASE_URL || !SERVICE_ROLE) {
  console.error("Defina NEXT_PUBLIC_SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY no ambiente")
  process.exit(1)
}

const LORA_BUCKET = "loras"
const LORA_NAME = `acct-${ACCOUNT_ID}`
const storagePath = `${ACCOUNT_ID}/${VERSION}`

const db = createClient(SUPABASE_URL, SERVICE_ROLE, { auth: { persistSession: false } })

async function adminPost(path, body) {
  const res = await fetch(`${AGENT_URL}/admin${path}`, {
    method: "POST",
    headers: { "X-Admin-Secret": ADMIN_SECRET, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
  const text = await res.text()
  if (!res.ok) throw new Error(`${path} → ${res.status}: ${text}`)
  return JSON.parse(text)
}

console.log(`\n== Medição de load do adapter ${LORA_NAME} (${LORA_BUCKET}/${storagePath}) ==\n`)

// 1. Signed URLs dos arquivos do adapter
// Whitelist PEFT — manter em sincronia com docker/agent/main.py e lib/actions.ts
const REQUIRED = ["adapter_config.json", "adapter_model.safetensors"]
const ALLOWED = new Set([
  ...REQUIRED,
  "tokenizer.json",
  "tokenizer_config.json",
  "special_tokens_map.json",
  "added_tokens.json",
  "chat_template.jinja",
])

const { data: entries, error: listErr } = await db.storage.from(LORA_BUCKET).list(storagePath)
if (listErr) {
  console.error(`Falha ao listar ${LORA_BUCKET}/${storagePath}: ${listErr.message}`)
  process.exit(1)
}
// entradas com id são objetos reais; sem id são "pastas" (inclusive as criadas
// por engano via "Create folder" no dashboard — já aconteceu, fica o aviso)
const objects = (entries ?? []).filter((f) => f.id)
const folders = (entries ?? []).filter((f) => !f.id)
if (folders.length > 0) {
  console.warn(
    `Aviso: ${LORA_BUCKET}/${storagePath} contém pastas (${folders.map((f) => f.name || "(sem nome)").join(", ")}) — pastas são ignoradas; os adapters devem ser ARQUIVOS neste prefixo`
  )
}
const files = objects.filter((f) => ALLOWED.has(f.name))
const missing = REQUIRED.filter((r) => !files.some((f) => f.name === r))
if (missing.length > 0) {
  console.error(
    `Adapter incompleto em ${LORA_BUCKET}/${storagePath} — faltam: ${missing.join(", ")}\n` +
      `Objetos encontrados no prefixo: ${objects.map((f) => f.name).join(", ") || "(nenhum)"}\n` +
      `Faça upload dos ARQUIVOS do adapter (formato PEFT) diretamente em ${LORA_BUCKET}/${storagePath}/`
  )
  process.exit(1)
}
const signed = []
for (const f of files) {
  const { data, error } = await db.storage
    .from(LORA_BUCKET)
    .createSignedUrl(`${storagePath}/${f.name}`, 600)
  if (error) throw new Error(`Falha ao assinar ${f.name}: ${error.message}`)
  signed.push({ name: f.name, url: data.signedUrl })
}
console.log(`Arquivos: ${signed.map((s) => s.name).join(", ")}`)

// 2. Garante estado limpo (unload é idempotente)
await adminPost("/unload-lora", { lora_name: LORA_NAME }).catch(() => {})

// 3. Load cronometrado (fim a fim, do ponto de vista do chamador)
const t0 = performance.now()
const result = await adminPost("/load-lora", { lora_name: LORA_NAME, files: signed })
const totalS = ((performance.now() - t0) / 1000).toFixed(2)

console.log(`\nResultado:`)
console.log(`  download (storage → disco do pod): ${result.download_s}s`)
console.log(`  load (disco → VRAM no vLLM):       ${result.load_s}s`)
console.log(`  total fim a fim (com rede):        ${totalS}s`)

// 4. Confirma que o adapter aparece na lista
const loras = await fetch(`${AGENT_URL}/admin/loras`, {
  headers: { "X-Admin-Secret": ADMIN_SECRET },
}).then((r) => r.json())
console.log(`\nAdapters carregados no vLLM: ${loras.loras.join(", ") || "(nenhum)"}`)
if (!loras.loras.includes(LORA_NAME)) {
  console.error("ERRO: adapter não aparece na lista após o load")
  process.exit(1)
}

// 5. Inferência de teste (opcional, requer --api-key)
if (API_KEY) {
  const prompt = "Escreva uma função em Python que soma dois números."
  for (const model of [LORA_NAME, ...(MODEL_BASE ? [MODEL_BASE] : [])]) {
    const t = performance.now()
    const res = await fetch(`${AGENT_URL}/v1/chat/completions`, {
      method: "POST",
      headers: { Authorization: `Bearer ${API_KEY}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        model,
        messages: [{ role: "user", content: prompt }],
        max_tokens: 100,
      }),
    })
    const json = await res.json()
    const ms = (performance.now() - t).toFixed(0)
    const preview = json.choices?.[0]?.message?.content?.slice(0, 80)?.replace(/\n/g, " ")
    console.log(`\n[${model}] ${res.status} em ${ms}ms → ${preview ?? JSON.stringify(json).slice(0, 120)}`)
  }
}

console.log("\nOK.")
