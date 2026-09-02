// Move o GO-Teste entre os estados do experimento. UMA variável por transição.
//
// O template é único e muda de estado (decisão do usuário), então o risco de
// atribuir uma medição à config errada é real: no Pro o log de boot dizia
// spec_tokens 5 enquanto o script gravava 3. Daí as duas travas aqui:
//   1. aborta se o template não estiver EXATAMENTE no estado de origem esperado
//   2. verify de leitura de volta no Supabase E no RunPod
//
// E o env do pod é congelado na criação (podInputFromTemplate passa env
// explícito, sem templateId): depois de editar, o pod tem que ser RECRIADO.
// Start/stop mantém o env antigo.
//
//   node --env-file=.env scripts/_tmp-go-teste-fase.mjs B   # volta pra B (rollback do MTP)
//   node --env-file=.env scripts/_tmp-go-teste-fase.mjs C   # B + MTP2
//   node --env-file=.env scripts/_tmp-go-teste-fase.mjs D   # C + batch 4096

import { createClient } from "@supabase/supabase-js"

const TEMPLATE = "GO-Teste"

const COMUM =
  "--max-model-len 65536 --gpu-memory-utilization 0.90 --kv-cache-dtype fp8 " +
  "--max-num-seqs 16"
const CAUDA =
  "--served-model-name go-base --enable-prompt-tokens-details " +
  '--limit-mm-per-prompt {"image":4,"video":0}'
const MTP2 = '--speculative-config {"method":"mtp","num_speculative_tokens":2}'

// JSON sem espaços e sem aspas simples: docker/entrypoint.sh:121 expande
// ${VLLM_EXTRA_ARGS} SEM quotes, então espaço quebra o parse e aspas simples
// entram literalmente no argumento.
const ESTADOS = {
  B: `${COMUM} ${CAUDA}`,
  C: `${COMUM} ${MTP2} ${CAUDA}`,
  D: `${COMUM} --max-num-batched-tokens 4096 ${MTP2} ${CAUDA}`,
}

// de onde cada fase pode partir — impede pular etapa e perder a atribuição
const ORIGEM_ESPERADA = { C: ["B"], D: ["C"], B: ["C", "D"] }

const fase = (process.argv[2] || "").toUpperCase()
if (!ESTADOS[fase]) {
  console.error(`uso: node --env-file=.env scripts/_tmp-go-teste-fase.mjs <B|C|D>`)
  process.exit(1)
}

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
  if (!res.ok) {
    throw new Error(`RunPod ${rest.method ?? "GET"} ${path} → ${res.status}: ${await res.text()}`)
  }
  return res.status === 204 ? undefined : res.json()
}

const { data: tpl, error } = await db
  .from("templates").select("*").eq("name", TEMPLATE).single()
if (error) throw new Error(`template ${TEMPLATE} não encontrado: ${error.message}`)

const atual = tpl.env.VLLM_EXTRA_ARGS
const estadoAtual = Object.entries(ESTADOS).find(([, v]) => v === atual)?.[0] ?? "DESCONHECIDO"

console.log(`template : ${tpl.name} (${tpl.id})`)
console.log(`estado   : ${estadoAtual}  ->  ${fase}`)

if (estadoAtual === fase) {
  console.log("\njá está nesse estado — nada a fazer.")
  process.exit(0)
}
if (estadoAtual === "DESCONHECIDO") {
  console.error("\nABORTADO: o template não está em nenhum estado conhecido do experimento.")
  console.error("  atual:", atual)
  console.error("  Editar à mão quebra a atribuição das medições. Confira antes.")
  process.exit(1)
}
if (!ORIGEM_ESPERADA[fase].includes(estadoAtual)) {
  console.error(`\nABORTADO: ${fase} tem que partir de ${ORIGEM_ESPERADA[fase].join(" ou ")}, não de ${estadoAtual}.`)
  console.error("  Pular etapa faz a fase medir mais de uma variável.")
  process.exit(1)
}

const novo = ESTADOS[fase]
console.log(`\nargs antes : ${atual}`)
console.log(`args depois: ${novo}`)

// diff explícito: o que muda tem que ser UMA coisa
const antes = new Set(atual.split(/\s+(?=--)/))
const depois = new Set(novo.split(/\s+(?=--)/))
const saiu = [...antes].filter((f) => !depois.has(f))
const entrou = [...depois].filter((f) => !antes.has(f))
console.log(`\ndelta      : ${saiu.length ? "− " + saiu.join(" ; ") : "(nada sai)"}`)
console.log(`             ${entrou.length ? "+ " + entrou.join(" ; ") : "(nada entra)"}`)
if (saiu.length + entrou.length !== 1) {
  console.error(`\nABORTADO: a transição mexe em ${saiu.length + entrou.length} flags, não em 1.`)
  process.exit(1)
}

const env = { ...tpl.env, VLLM_EXTRA_ARGS: novo }

// RunPod primeiro, Supabase depois — mesma ordem de createTemplate
if (tpl.runpod_template_id) {
  await runpod(`/templates/${tpl.runpod_template_id}`, { method: "PATCH", json: { env } })
  console.log(`\nRunPod ${tpl.runpod_template_id} atualizado`)
}

const { data: upd, error: updErr } = await db
  .from("templates").update({ env }).eq("id", tpl.id).select("*").single()
if (updErr) throw new Error(`update falhou: ${updErr.message}`)

// verify nos dois lados — updateTemplate engole falha do RunPod em silêncio
let rpEnv = null
if (tpl.runpod_template_id) {
  try {
    rpEnv = (await runpod(`/templates/${tpl.runpod_template_id}`)).env ?? null
  } catch (e) {
    console.error("falha ao reler no RunPod:", e.message)
  }
}

const temMtp = novo.includes("speculative-config")
const checks = {
  "Supabase com os args da fase": upd.env.VLLM_EXTRA_ARGS === novo,
  "RunPod com os args da fase": rpEnv ? rpEnv.VLLM_EXTRA_ARGS === novo : false,
  [`MTP ${temMtp ? "presente" : "ausente"} como a fase pede`]:
    novo.includes("speculative-config") === temMtp,
  "modelo segue o w4a16": upd.model_name === "RedHatAI/Qwen3.5-9B-quantized.w4a16",
  "janela segue 65536": novo.includes("--max-model-len 65536"),
  "max-num-seqs segue 16": novo.includes("--max-num-seqs 16"),
  "alias segue go-base": novo.includes("--served-model-name go-base"),
  "is_test segue true": upd.is_test === true,
  "sem aspas simples no JSON": !novo.includes("'"),
  "JSON do spec sem espaços": !/"method":\s|,\s"num_speculative/.test(novo),
}
console.log()
for (const [k, ok] of Object.entries(checks)) console.log(`  ${ok ? "ok   " : "FALHA"} ${k}`)
if (Object.values(checks).some((ok) => !ok)) {
  console.error("\nverify falhou — não recrie o pod ainda")
  process.exit(1)
}

console.log(`
fase ${fase} gravada nos dois lados.

RECRIE o pod — o env é congelado na criação, start/stop não basta. E no boot
confira a linha "non-default args" do /admin/logs?tail=2000 (header
x-admin-secret): ela é o registro do que foi medido, não o template.
  fase B -> speculative_config=None
  fase C -> SpeculativeConfig(method='mtp', num_spec_tokens=2)
`)
