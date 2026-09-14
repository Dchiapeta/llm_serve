// Prepara o GO-IMAGE-A40-QUALITY para a grade casada da flux2-klein-4b-0.1.7.
//
//   node --env-file=.env scripts/_tmp-image-quality-grade-casada.mjs [--apply]
//
// Sem --apply é dry-run. Pod existente NÃO herda env de template: recriar a
// máquina depois (e apontar a imagem para a 0.1.7 quando ela for publicada).
//
// Por que cada valor (bateria de try-on de 14/09/2026):
//
// - 816x1216 entra na allowlist. A 0.1.7, sem `size`, só escolhe canvas ≤ 1 MP
//   (acima disso o pipeline reduz a referência e a grade volta a divergir). Sem
//   um retrato ≤ 1 MP na lista, uma selfie cai no 1024x1024 com padding lateral
//   grande e a pessoa sai com menos pixels. 816x1216 é 2:3, múltiplo de 16 e
//   0,99 MP.
//
//   AVISO: a lista passa de 6 para 7 resoluções. O comentário do
//   _tmp-update-image-quality-template.mjs lembra que 7 × 5 (refs 0–4) = 35 séries
//   passa de policy.MAX_SCENARIO_SERIES=32. Só estoura se as 35 combinações forem
//   de fato usadas; num teste serial de try-on não chega perto.
//
// - IMAGE_STEPS 8 -> 4. O Klein 4B destilado é travado em 4 steps e guidance 1
//   no repo oficial da BFL, e o `mu` do scheduler depende de steps. O teto
//   (IMAGE_STEPS_MAX) fica onde está, então 8 continua pedível por request.

import { createClient } from "@supabase/supabase-js"

const NAME = "GO-IMAGE-A40-QUALITY"
const APPLY = process.argv.includes("--apply")
const RUNPOD_REST = "https://rest.runpod.io/v1"
const NOVO_TAMANHO = "816x1216"

const db = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL,
  process.env.SUPABASE_SERVICE_ROLE_KEY,
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
  if (!res.ok) {
    throw new Error(`RunPod ${rest.method ?? "GET"} ${path} → ${res.status}: ${await res.text()}`)
  }
  return res.status === 204 ? undefined : res.json()
}

const { data: tpl, error: readErr } = await db
  .from("templates")
  .select("*")
  .eq("name", NAME)
  .maybeSingle()
if (readErr) throw new Error(`leitura falhou: ${readErr.message}`)
if (!tpl) {
  console.error(`template ${NAME} não existe no Supabase.`)
  process.exit(1)
}

console.log(`${NAME}  (${tpl.id})`)
console.log(`  runpod_template_id: ${tpl.runpod_template_id ?? "—"}`)
console.log(`  image: ${tpl.image}\n`)

const before = tpl.env ?? {}
// Acrescenta à lista que JÁ está gravada, em vez de reescrevê-la: não mexe nas
// resoluções que outros testes usam.
const sizes = (before.IMAGE_ALLOWED_SIZES ?? "1024x1024,1536x1024,1024x1536")
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean)
if (!sizes.includes(NOVO_TAMANHO)) sizes.push(NOVO_TAMANHO)

const PATCH = {
  IMAGE_ALLOWED_SIZES: sizes.join(","),
  IMAGE_STEPS: "4",
}
const after = { ...before, ...PATCH }

console.log("diff de env:")
let mudou = false
for (const [k, v] of Object.entries(PATCH)) {
  if (before[k] === v) {
    console.log(`  = ${k}: ${v}  (já estava)`)
  } else {
    mudou = true
    console.log(`  ~ ${k}: ${before[k] ?? "—"}  ->  ${v}`)
  }
}
if (!mudou) {
  console.log("\nnada a fazer.")
  process.exit(0)
}
if (!APPLY) {
  console.log("\n[dry-run] nada foi escrito. Rode com --apply para aplicar.")
  process.exit(0)
}

// Supabase primeiro: é o que o provisionamento lê (podInputFromTemplate).
const { error: upErr } = await db.from("templates").update({ env: after }).eq("id", tpl.id)
if (upErr) throw new Error(`update no Supabase falhou: ${upErr.message}`)
console.log("\nSupabase atualizado.")

if (tpl.runpod_template_id) {
  try {
    await runpod(`/templates/${tpl.runpod_template_id}`, { method: "PATCH", json: { env: after } })
    console.log("RunPod atualizado.")
  } catch (e) {
    console.error(`\nAVISO: Supabase atualizado mas o RunPod NÃO: ${e.message}`)
  }
}

// Verify por leitura de volta nos dois lados — nunca pela resposta do update.
const { data: row, error: vErr } = await db.from("templates").select("env").eq("id", tpl.id).single()
if (vErr) throw new Error(`leitura de volta do Supabase falhou: ${vErr.message}`)
let rp = null
if (tpl.runpod_template_id) {
  try {
    rp = await runpod(`/templates/${tpl.runpod_template_id}`)
  } catch (e) {
    console.error(`leitura de volta do RunPod falhou: ${e.message}`)
  }
}

console.log("\nverify:")
let ok = true
for (const [k, v] of Object.entries(PATCH)) {
  const sbOk = row.env?.[k] === v
  const rpOk = rp === null ? null : rp.env?.[k] === v
  if (!sbOk || rpOk === false) ok = false
  const marca = (b) => (b === null ? "?" : b ? "ok" : "FALHOU")
  console.log(`  ${k}: supabase=${marca(sbOk)} runpod=${marca(rpOk)}`)
}
console.log(ok ? "\nTudo gravado. Recriar a máquina do template para ter efeito." : "\nAlgum lado NÃO gravou.")
process.exit(ok ? 0 : 1)
