// Corrige o template de PRODUÇÃO de imagem (GO-IMAGE-A40).
//
// Não é ajuste fino: o item 1 é um bug medido em produção. Os três valores
// abaixo são os únicos com evidência; o que não tem medição (TF32, resoluções
// maiores, steps > 8, n > 1) fica de fora de propósito e vive só no template
// de teste GO-IMAGE-A40-QUALITY, que existe para medir exatamente isso.
//
//   node --env-file=.env scripts/_tmp-fix-image-prod-template.mjs [--apply]
//
// Sem --apply é dry-run.
//
// DEPOIS DE APLICAR: a máquina precisa ser RECRIADA. Template atualizado não
// reconfigura pod em execução — foi assim que o PRO ficou servindo 16K com o
// banco dizendo 32K.

import { createClient } from "@supabase/supabase-js"

const NAME = "GO-IMAGE-A40"
const APPLY = process.argv.includes("--apply")
const RUNPOD_REST = "https://rest.runpod.io/v1"

const PATCH = {
  // 1. O BUG. O template de produção está SEM esta variável, então o pod
  // sorteia a seed a cada request — confirmado medindo o gateway em
  // 13/09/2026: duas requisições idênticas devolveram meta.seed
  // 2530228968386470472 e 6720345906978460992.
  //
  // Por que isso é grave e não é só "variância": medido em 10/09, ~30% das
  // seeds DESCARTAM a foto do cliente e devolvem a pessoa da imagem de
  // referência — rosto, corpo e cenário. Não é degradação gradual, é a foto
  // de outra pessoa. Nenhum prompt impede.
  //
  // A contrapartida é real e foi aceita conscientemente: com seed fixa, o
  // cliente que cair num caso ruim vê o mesmo resultado ruim toda vez, sem
  // "tenta de novo". A alternativa (gerar N e filtrar) precisa de um juiz
  // automático de identidade, que não existe. A 31337 passou nas 4 condições
  // testadas (foto original, espelhada, recortada e o par vestido/modelo de
  // 13/09).
  IMAGE_DEFAULT_SEED: "31337",

  // 2. 4 -> 8 steps. Ganho medido em 10/09: o 8 resolve o laceamento e as
  // franjas da peça, que o 4 entrega como textura borrada; a pose não muda.
  //
  // CUSTO, que é o motivo de isto não ter sido feito antes: dobra o tempo de
  // GPU por geração (~8 s -> ~15,5 s a 1024x1536 com 2 referências), logo
  // dobra o custo por request e reduz a vazão da máquina pela metade. Com
  // IMAGE_QUEUE_CAPACITY=3, três requests em fila passam a somar ~47 s contra
  // os 90 s do gateway — ainda cabe, mas a folga encolheu.
  IMAGE_STEPS: "8",

  // 3. 5 -> 10 MiB por arquivo de referência. Medido em 13/09: a resolução da
  // referência decide a FIDELIDADE da peça. Degradando só o vestido de 998 px
  // para 499 px, o laceamento some e vira textura genérica; a 249 px o modelo
  // devolve outro vestido. A referência entra como tokens visuais (o tempo
  // acompanha: 14,4 -> 15,9 -> 19,8 s), então o teto de arquivo é teto de
  // qualidade, não só de upload.
  //
  // ATENÇÃO ao efeito colateral: o gateway recusa o multipart inteiro acima de
  // MAX_IMAGE_EDIT_BYTES (default 4 x 5 MiB + 1 = 21 MiB, em
  // docker/gateway/image_proxy.py). Subir o arquivo para 10 MiB sem subir esse
  // teto trocaria "4 referências de 5 MiB" por "2 de 10 MiB" — regressão
  // silenciosa para quem usa 3 ou 4 referências, com um 413 vindo do gateway
  // que o pod nem vê. Por isso o default do gateway sobe junto, no mesmo
  // commit, e o gateway precisa ser publicado ANTES ou junto deste patch.
  IMAGE_MAX_FILE_SIZE_MB: "10",
}

// Deliberadamente FORA deste patch:
//   IMAGE_ALLOW_TF32        — ganho nunca medido isoladamente
//   IMAGE_STEPS_MAX         — 8 em produção evita o cliente pedir 20 e estourar os 90 s
//   IMAGE_ALLOWED_SIZES     — resoluções maiores não têm custo medido
//   IMAGE_MAX_SEQUENCE_LENGTH, IMAGE_IMAGES_PER_REQUEST_MAX — features, não correções

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
    throw new Error(
      `RunPod ${rest.method ?? "GET"} ${path} → ${res.status}: ${await res.text()}`,
    )
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
  console.error(`template ${NAME} não existe — nada a atualizar.`)
  process.exit(1)
}

console.log(`${NAME}  (${tpl.id})`)
console.log(`  runpod_template_id: ${tpl.runpod_template_id ?? "—"}`)
console.log(`  is_test=${tpl.is_test}  is_enabled=${tpl.is_enabled}  max_users=${tpl.max_users}`)
console.log(`  image: ${tpl.image}\n`)

const before = tpl.env ?? {}
const after = { ...before, ...PATCH }

console.log("diff de env:")
let mudou = false
for (const [k, v] of Object.entries(PATCH)) {
  const old = before[k]
  if (old === v) console.log(`  = ${k}: ${v}  (já estava)`)
  else {
    mudou = true
    console.log(`  ~ ${k}: ${old ?? "AUSENTE (sorteia)"}  ->  ${v}`)
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

const { error: upErr } = await db.from("templates").update({ env: after }).eq("id", tpl.id)
if (upErr) throw new Error(`update no Supabase falhou: ${upErr.message}`)
console.log("\nSupabase atualizado.")

if (!tpl.runpod_template_id) {
  console.log("sem runpod_template_id — só o registro local foi atualizado.")
} else {
  try {
    await runpod(`/templates/${tpl.runpod_template_id}`, { method: "PATCH", json: { env: after } })
    console.log("RunPod atualizado.")
  } catch (e) {
    console.error(`\nAVISO: Supabase atualizado, RunPod NÃO: ${e.message}`)
  }
}

// verify por leitura de volta nos dois lados — o updateTemplate do painel
// engole erro em silêncio (lib/actions.ts:383).
const { data: row, error: vErr } = await db
  .from("templates").select("env").eq("id", tpl.id).single()
if (vErr) throw new Error(`leitura de volta falhou: ${vErr.message}`)

let rp = null
if (tpl.runpod_template_id) {
  try { rp = await runpod(`/templates/${tpl.runpod_template_id}`) }
  catch (e) { console.error(`leitura de volta do RunPod falhou: ${e.message}`) }
}

console.log("\nverify:")
let ok = true
for (const [k, v] of Object.entries(PATCH)) {
  const sbOk = row.env?.[k] === v
  const rpOk = rp === null ? null : rp?.env?.[k] === v
  if (!sbOk || rpOk === false) ok = false
  const marca = (b) => (b === null ? "?" : b ? "ok" : "FALHOU")
  console.log(`  ${k}: supabase=${marca(sbOk)} runpod=${marca(rpOk)}`)
}

console.log(
  ok
    ? "\nTudo gravado.\n\n" +
        "FALTA (é você quem sobe o pod): RECRIAR a máquina de produção deste\n" +
        "template. Até lá o pod segue sorteando a seed e gerando com 4 steps.\n" +
        "E publicar o gateway com o teto de multipart novo, senão 3+ referências\n" +
        "passam a tomar 413."
    : "\nAlgum lado NÃO gravou. Não recrie a máquina antes de resolver.",
)
process.exit(ok ? 0 : 1)
