// Solta os tetos de qualidade do template GO-IMAGE-A40-QUALITY.
//
// Por que só neste template: os tetos abaixo são escolhas de CUSTO DE GPU, e
// nenhum deles tem ganho medido — subir em produção seria trocar um pod de
// ~20 s por um de tempo desconhecido às cegas. Aqui eles viram uma faixa
// MEDÍVEL: o default de cada parâmetro continua onde está, só o teto sobe, e
// quem escolhe o ponto de operação é cada request do teste.
//
//   node --env-file=.env scripts/_tmp-update-image-quality-template.mjs [--apply]
//
// Sem --apply é dry-run: imprime o diff e não escreve em lugar nenhum.
//
// DEPOIS DE APLICAR: o pod existente NÃO herda env de template atualizado
// (mesma lição do PRO que ficou em 16K com o Supabase dizendo 32K). É preciso
// RECRIAR a máquina para que qualquer coisa aqui tenha efeito.

import { createClient } from "@supabase/supabase-js"

const NAME = "GO-IMAGE-A40-QUALITY"
const APPLY = process.argv.includes("--apply")
const RUNPOD_REST = "https://rest.runpod.io/v1"

// ---------- o que muda, e o teto real de cada eixo ----------
//
// Cada valor esbarra num limite de OUTRA camada. Documentados aqui porque o
// sintoma de estourá-los (504, 413, métrica sumindo) não aponta para este
// arquivo.
const PATCH = {
  // 8 -> 20. O checkpoint é distilled (is_distilled: true), então o retorno
  // acima de 8 é plausível mas desconhecido — o 8 foi escolhido como teto sem
  // nunca se medir o 12 ou o 16.
  //
  // TETO REAL: o gateway corta a request em IMAGE_UPSTREAM_TIMEOUT_S=90 s, e
  // essa janela inclui a espera na fila. A 1024x1536 e 2 referências, 8 steps
  // custam ~15,5 s de GPU, então 20 steps ficam em ~39 s — cabe. O que NÃO
  // cabe é combinar 20 steps com as resoluções novas (ver abaixo).
  IMAGE_STEPS_MAX: "20",

  // 3 -> 6 resoluções. A progressão em retrato (1024x1536 -> 1280x1920 ->
  // 1536x2304) é o eixo a medir: 1,57 -> 2,46 -> 3,54 MP. O 1536x1536 entra
  // para o mesmo eixo em quadrado, já que a pergunta é de qualidade geral e
  // não de try-on.
  //
  // SEIS e não mais: policy.MAX_SCENARIO_SERIES é 32 e o agregador do
  // /metrics indexa por (size, refs). Com refs de 0 a 4 são 5 valores por
  // resolução, então 6 x 5 = 30 séries. A sétima resolução passaria de 32 e o
  // /metrics começaria a DESCARTAR cenários em silêncio (dropped_series) —
  // justamente a medição de VRAM e tempo que motiva esta mudança.
  //
  // Todas múltiplas de 16 (VAE scale 8 x patch 2); um valor fora disso é
  // recusado ou arredondado pelo pipeline.
  IMAGE_ALLOWED_SIZES:
    "1024x1024,1536x1024,1024x1536,1536x1536,1280x1920,1536x2304",

  // 512 -> 1024 tokens de prompt. O 512 é o default histórico do FLUX com T5;
  // o FLUX.2 usa um encoder Mistral, que comporta muito mais. Vale porque
  // prompt de precisão gasta tokens enumerando o que preservar.
  //
  // A VALIDAR na primeira geração após o pod subir: se o encoder recusar o
  // valor, o sintoma aparece na geração, não no boot.
  IMAGE_MAX_SEQUENCE_LENGTH: "1024",

  // 1 -> 3. Não é vazão: é trocar a loteria de seed por escolha, gerando N e
  // ficando com a melhor. Custa N x GPU na MESMA request — n=3 a 8 steps e
  // 1024x1536 fica em ~47 s, ainda dentro dos 90 s do gateway; n=3 com
  // resolução grande não fica.
  IMAGE_IMAGES_PER_REQUEST_MAX: "3",

  // 5 -> 10 MiB por arquivo de referência. Único item da lista com ganho
  // MEDIDO: degradar a referência do vestido de 998 px para 499 px apaga o
  // laceamento da peça, e para 249 px devolve outro vestido. A referência
  // entra como tokens visuais (o tempo de GPU acompanha: 14,4 -> 15,9 ->
  // 19,8 s), então resolução de referência é fidelidade, e o teto de arquivo
  // é o que limita o que o cliente consegue mandar.
  //
  // TETO REAL: o gateway recusa o multipart acima de MAX_IMAGE_EDIT_BYTES,
  // cujo default é 4 x 5 MiB + 1 MiB = 21 MiB (image_proxy.py). Com 10 MiB por
  // arquivo, DUAS referências cabem (20 MiB) e três não — e o 413 vem do
  // gateway, que o pod nunca vê. Try-on usa duas. Para usar as quatro, subir
  // MAX_IMAGE_EDIT_BYTES no Railway para ~41 MiB.
  IMAGE_MAX_FILE_SIZE_MB: "10",
}

// Mantidos de propósito, para que o diff de qualidade não venha misturado:
//   IMAGE_STEPS=8, IMAGE_ALLOW_TF32=false, IMAGE_DEFAULT_SEED=31337
//   IMAGE_QUEUE_CAPACITY=3, IMAGE_QUEUE_WAIT_TIMEOUT_S=60
// O capacity fica em 3 sabendo que gerações mais longas tornam a espera de
// fila mais provável; como max_users=1, o teste é serial e a fila não enche.

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

// ---------- 1. lê o estado atual ----------

const { data: tpl, error: readErr } = await db
  .from("templates")
  .select("*")
  .eq("name", NAME)
  .maybeSingle()

if (readErr) throw new Error(`leitura falhou: ${readErr.message}`)
if (!tpl) {
  console.error(`template ${NAME} não existe no Supabase — nada a atualizar.`)
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
  if (old === v) {
    console.log(`  = ${k}: ${v}  (já estava)`)
  } else {
    mudou = true
    console.log(`  ~ ${k}: ${old ?? "—"}  ->  ${v}`)
  }
}

if (!mudou) {
  console.log("\nnada a fazer: o env já está no alvo.")
  process.exit(0)
}

if (!APPLY) {
  console.log("\n[dry-run] nada foi escrito. Rode de novo com --apply para aplicar.")
  process.exit(0)
}

// ---------- 2. Supabase ----------
//
// Supabase primeiro pelo mesmo motivo do script de criação: se ele falhar,
// nada divergiu. RunPod primeiro deixaria o espelho adiantado em relação ao
// registro que o provisionamento realmente lê.

const { error: upErr } = await db
  .from("templates")
  .update({ env: after })
  .eq("id", tpl.id)

if (upErr) throw new Error(`update no Supabase falhou: ${upErr.message}`)
console.log("\nSupabase atualizado.")

// ---------- 3. RunPod ----------

if (!tpl.runpod_template_id) {
  console.log("sem runpod_template_id — só o registro local foi atualizado.")
} else {
  try {
    await runpod(`/templates/${tpl.runpod_template_id}`, {
      method: "PATCH",
      json: { env: after },
    })
    console.log("RunPod atualizado.")
  } catch (e) {
    console.error(
      `\nAVISO: Supabase atualizado mas o RunPod NÃO: ${e.message}\n` +
        "Os dois lados estão divergentes. O provisionamento passa env explícito\n" +
        "a partir do Supabase (podInputFromTemplate), então a máquina nova sai\n" +
        "correta — mas o console do RunPod mostra o env antigo.",
    )
  }
}

// ---------- 4. verify por leitura de volta, nos DOIS lados ----------
//
// SELECT novo e GET novo, nunca a resposta do update: o updateTemplate do
// painel engole erro em silêncio (lib/actions.ts:383), e foi assim que o PRO
// ficou servindo 16K com o banco dizendo 32K. A única forma de saber o que
// está gravado é perguntar.

const { data: row, error: vErr } = await db
  .from("templates")
  .select("env")
  .eq("id", tpl.id)
  .single()
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
  const sb = row.env?.[k]
  const runpodVal = rp?.env?.[k]
  const sbOk = sb === v
  const rpOk = rp === null ? null : runpodVal === v
  if (!sbOk || rpOk === false) ok = false
  const marca = (b) => (b === null ? "?" : b ? "ok" : "FALHOU")
  console.log(`  ${k}: supabase=${marca(sbOk)} runpod=${marca(rpOk)}`)
}

console.log(
  ok
    ? "\nTudo gravado.\n\n" +
        "PRÓXIMO PASSO (é você quem sobe o pod): RECRIAR a máquina deste\n" +
        "template. Máquina existente não herda env de template atualizado —\n" +
        "até recriar, o pod continua com steps_max=8, 3 resoluções e 512 tokens."
    : "\nAlgum lado NÃO gravou. Não recrie a máquina antes de resolver.",
)
process.exit(ok ? 0 : 1)
