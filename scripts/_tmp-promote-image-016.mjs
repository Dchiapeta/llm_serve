// Aponta os templates de imagem para a flux2-klein-4b-0.1.6.
//
//   node --env-file=.env scripts/_tmp-promote-image-016.mjs [--apply]
//
// Sem --apply é dry-run.
//
// O que a 0.1.6 traz: bloco `usage` na resposta de /v1/images/generations e
// /v1/images/edits, com o consumo em tokens (um por patch latente de 16×16 px
// mais os tokens do prompt). É o que faz o agent somar imagem em usage_metrics
// e o gateway gravar tokens_in/out. Nenhuma mudança na geração. Os DOIS
// templates vão juntos: deixar um para trás faria a stack roteada para ele
// voltar a aparecer com consumo zero em tokens.
//
// DEPOIS DE APLICAR: recriar as máquinas. Template atualizado não troca a
// imagem de um pod em execução.

import { createClient } from "@supabase/supabase-js"

const NOVA = "dchiapeta/diffusers-agent:flux2-klein-4b-0.1.6"
const ANTERIOR = "dchiapeta/diffusers-agent:flux2-klein-4b-0.1.5"
const NOMES = ["GO-IMAGE-A40", "GO-IMAGE-A40-QUALITY"]
const APPLY = process.argv.includes("--apply")
const RUNPOD_REST = "https://rest.runpod.io/v1"

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

// ---------- 1. a tag existe mesmo no registry? ----------
//
// Não há EXPECTED_DIGEST a comparar aqui como no script de criação: a 0.1.6
// acabou de ser publicada e este script é o primeiro a vê-la. O que se pode
// verificar é que ela EXISTE — apontar um template para uma tag que o push não
// completou daria um pod que não sobe, e o erro apareceria só no boot, longe
// daqui. O digest resolvido é impresso para ir ao README.
async function resolveTagDigest(repo, tag) {
  const auth = await fetch(
    `https://auth.docker.io/token?service=registry.docker.io&scope=repository:${repo}:pull`,
  )
  if (!auth.ok) throw new Error(`auth do registry falhou: ${auth.status}`)
  const { token } = await auth.json()
  const res = await fetch(`https://registry-1.docker.io/v2/${repo}/manifests/${tag}`, {
    method: "HEAD",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
      ].join(", "),
    },
  })
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`registry devolveu ${res.status}`)
  return res.headers.get("docker-content-digest")
}

const [repo, tag] = NOVA.split(":")
let digest = null
try {
  digest = await resolveTagDigest(repo, tag)
} catch (e) {
  console.error(`não foi possível consultar o registry: ${e.message}`)
  console.error("Rode com IMAGE_CHECK=skip para seguir assim mesmo.")
  if (process.env.IMAGE_CHECK !== "skip") process.exit(1)
}
if (digest === null && process.env.IMAGE_CHECK !== "skip") {
  console.error(`\nERRO: a tag ${NOVA} não existe no registry.`)
  console.error("O build+push terminou mesmo? Ver docker/image/README.md.")
  process.exit(1)
}
console.log(`${NOVA}`)
console.log(`  digest: ${digest ?? "(não verificado)"}\n`)

// ---------- 2. estado atual ----------

const { data: rows, error: readErr } = await db
  .from("templates")
  .select("id,name,image,runpod_template_id")
  .in("name", NOMES)

if (readErr) throw new Error(`leitura falhou: ${readErr.message}`)

const alvos = []
for (const nome of NOMES) {
  const row = rows.find((r) => r.name === nome)
  if (!row) {
    console.error(`ERRO: template ${nome} não existe.`)
    process.exit(1)
  }
  if (row.image === NOVA) {
    console.log(`  = ${nome}: já está na 0.1.6`)
  } else if (row.image !== ANTERIOR) {
    // Não é erro, mas é surpresa: alguém mexeu na tag fora deste fluxo, e
    // sobrescrever em silêncio esconderia isso.
    console.log(`  ! ${nome}: está em ${row.image} (esperado ${ANTERIOR}) — será trocado mesmo assim`)
    alvos.push(row)
  } else {
    console.log(`  ~ ${nome}: ${row.image}  ->  ${NOVA}`)
    alvos.push(row)
  }
}

if (alvos.length === 0) {
  console.log("\nnada a fazer.")
  process.exit(0)
}

if (!APPLY) {
  console.log("\n[dry-run] nada foi escrito. Rode com --apply para aplicar.")
  process.exit(0)
}

// ---------- 3. aplica nos dois lados ----------

for (const row of alvos) {
  const { error } = await db.from("templates").update({ image: NOVA }).eq("id", row.id)
  if (error) throw new Error(`update de ${row.name} falhou: ${error.message}`)
  console.log(`\n${row.name}: Supabase atualizado.`)

  if (!row.runpod_template_id) {
    console.log(`${row.name}: sem runpod_template_id — só o registro local.`)
    continue
  }
  try {
    await runpod(`/templates/${row.runpod_template_id}`, {
      method: "PATCH",
      json: { imageName: NOVA },
    })
    console.log(`${row.name}: RunPod atualizado.`)
  } catch (e) {
    console.error(`${row.name}: AVISO — Supabase atualizado, RunPod NÃO: ${e.message}`)
  }
}

// ---------- 4. verify por leitura de volta ----------

const { data: depois, error: vErr } = await db
  .from("templates")
  .select("name,image,runpod_template_id")
  .in("name", NOMES)
if (vErr) throw new Error(`leitura de volta falhou: ${vErr.message}`)

console.log("\nverify:")
let ok = true
for (const row of depois) {
  const sbOk = row.image === NOVA
  let rpOk = null
  if (row.runpod_template_id) {
    try {
      const rp = await runpod(`/templates/${row.runpod_template_id}`)
      rpOk = rp?.imageName === NOVA
    } catch (e) {
      console.error(`  (leitura do RunPod de ${row.name} falhou: ${e.message})`)
    }
  }
  if (!sbOk || rpOk === false) ok = false
  const marca = (b) => (b === null ? "?" : b ? "ok" : "FALHOU")
  console.log(`  ${row.name}: supabase=${marca(sbOk)} runpod=${marca(rpOk)}`)
}

console.log(
  ok
    ? "\nTudo gravado.\n\n" +
        "FALTA (é você quem sobe o pod): RECRIAR as máquinas dos dois templates.\n" +
        "Até recriar, os pods seguem rodando a 0.1.5 — sem `usage`, e as stacks\n" +
        "de imagem continuam com tokens nulos nos painéis.\n\n" +
        `Registre o digest no README: ${digest ?? "(não verificado)"}`
    : "\nAlgum lado NÃO gravou. Não recrie as máquinas antes de resolver.",
)
process.exit(ok ? 0 : 1)
