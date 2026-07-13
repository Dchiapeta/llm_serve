#!/usr/bin/env node
// Sobe os arquivos de um adapter LoRA local para o bucket "loras" na
// convenção {account_id}/{version}/. Alternativa segura ao dashboard
// (que cria pastas em vez de arquivos com facilidade).
//
// Uso:
//   node scripts/upload-lora.mjs --account-id <uuid> --version v1 --dir ./meu-adapter
//
// O diretório precisa conter adapter_config.json + adapter_model.safetensors
// (formato PEFT). Requer NEXT_PUBLIC_SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY
// no ambiente (ex: node --env-file=.env scripts/upload-lora.mjs ...).

import { readFile, readdir } from "node:fs/promises"
import { join } from "node:path"
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

const ACCOUNT_ID = arg("account-id")
const VERSION = arg("version", "v1")
const DIR = arg("dir")

const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL
const SERVICE_ROLE = process.env.SUPABASE_SERVICE_ROLE_KEY
if (!SUPABASE_URL || !SERVICE_ROLE) {
  console.error("Defina NEXT_PUBLIC_SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY no ambiente")
  process.exit(1)
}

const LORA_BUCKET = "loras"
const REQUIRED = ["adapter_config.json", "adapter_model.safetensors"]
const ALLOWED = new Set([
  ...REQUIRED,
  "tokenizer.json",
  "tokenizer_config.json",
  "special_tokens_map.json",
  "added_tokens.json",
  "chat_template.jinja",
])

const local = (await readdir(DIR, { withFileTypes: true }))
  .filter((e) => e.isFile() && ALLOWED.has(e.name))
  .map((e) => e.name)
const missing = REQUIRED.filter((r) => !local.includes(r))
if (missing.length > 0) {
  console.error(`Diretório ${DIR} não é um adapter PEFT completo — faltam: ${missing.join(", ")}`)
  process.exit(1)
}

const db = createClient(SUPABASE_URL, SERVICE_ROLE, { auth: { persistSession: false } })
const prefix = `${ACCOUNT_ID}/${VERSION}`

for (const name of local) {
  const content = await readFile(join(DIR, name))
  const contentType = name.endsWith(".json") ? "application/json" : "application/octet-stream"
  const { error } = await db.storage
    .from(LORA_BUCKET)
    .upload(`${prefix}/${name}`, content, { contentType, upsert: true })
  if (error) {
    console.error(`Falha no upload de ${name}: ${error.message}`)
    process.exit(1)
  }
  console.log(`✓ ${LORA_BUCKET}/${prefix}/${name} (${(content.length / 1024).toFixed(0)} KB)`)
}

console.log(`\nAdapter enviado. Registre no painel (Contas → Registrar LoRA) com versão "${VERSION}".`)
