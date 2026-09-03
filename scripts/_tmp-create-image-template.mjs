// Cria o template de TESTE de geração de imagem: FLUX.2 Klein 4B numa A40.
//
// Diferente dos outros _tmp-create-*, este NÃO é copy-with-override: não existe
// template de origem compatível (todos os atuais são vLLM), então os campos vão
// literais. O que se mantém do padrão daqueles scripts é o que importa:
//
//   - insere usage_class_config e is_test, que a Server Action NÃO escreve
//     (usage_class_config NULL = fail-open do machine_high_cap da migration 0037);
//   - verify por SELECT novo, pelo precedente do updateTemplate que engole erro
//     em silêncio (lib/actions.ts:383);
//   - idempotente: se o template já existe no Supabase, não faz nada.
//
// Duas coisas que ele faz DIFERENTE da Server Action, e por quê:
//
//   - Supabase primeiro, RunPod depois. A Server Action faz o inverso porque o
//     runpod_template_id faz parte do único insert dela; aqui, RunPod-primeiro
//     deixaria um template órfão no console do RunPod sempre que o insert
//     falhasse (e o modo de falha mais provável é o CHECK de plano, com a 0057
//     não aplicada).
//   - verifica no registry que a tag da imagem resolve para o digest que foi de
//     fato auditado. A 0.1.0 foi pushada antes de uma revisão e ficou no
//     registry com bugs; a existência da tag não diz nada sobre o conteúdo.
//
// PRÉ-REQUISITOS (nesta ordem):
//   1. supabase/migrations/0057_plan_image.sql aplicada — sem ela o CHECK
//      rejeita plan='Image' e o insert falha.
//   2. dchiapeta/diffusers-agent:flux2-klein-4b-0.1.2 publicada — o template
//      aponta para essa tag.
//
//   node --env-file=.env scripts/_tmp-create-image-template.mjs

import { createClient } from "@supabase/supabase-js"

const NAME = "IMAGE-A40-FLUX2-KLEIN-4B"

// Tag versionada e imutável, nunca :latest. Mudança de conteúdo é 0.1.1 — é o
// que torna apontar para a tag equivalente a apontar para o digest, e é a lição
// do template que ficou pinado em :v2 servindo uma imagem pré-tool-calling.
const IMAGE = "dchiapeta/diffusers-agent:flux2-klein-4b-0.1.2"

// Commit do repo no HF (lastModified 24/02/2026). Sem revision, o
// from_pretrained segue o `main` e o load test deixa de ser reproduzível se a
// Black Forest Labs republicar os pesos.
const MODEL_REVISION = "e7b7dc27f91deacad38e78976d1f2b499d76a294"

const TEMPLATE = {
  name: NAME,
  plan: "Image",
  image: IMAGE,
  model_name: "black-forest-labs/FLUX.2-klein-4B",
  gpu_types: ["NVIDIA A40"],
  gpu_count: 1,
  start_command: null,
  http_ports: ["8000"],
  tcp_ports: [],

  // Container disk: só a imagem (~4 GB de base + 29 pacotes). Os pesos vão
  // para o volume. Folgado de propósito nesta primeira versão — aperta depois
  // de medir.
  disk_gb: 80,

  // O RunPod reconstrói o container disk a cada START, não só na recriação. Com
  // volume_gb 0 o pod rebaixaria ~16 GB toda vez que a auto-pausa despausasse.
  // O HF_HOME abaixo aponta para cá.
  //
  // Ressalva: o volume morre junto com o pod, então recreateMachine ainda paga
  // o download inteiro — CreatePodInput (lib/runpod.ts) não expõe Network
  // Volume, que seria o que sobreviveria a isso.
  volume_gb: 40,
  volume_mount_path: "/models",

  // ---------------------------------------------------------------------
  // Capacidade: campos de KV cache usados a favor, não por acidente
  // ---------------------------------------------------------------------
  // model_footprint_gb e kv_reserve_gb_per_user NÃO têm significado em difusão
  // (não existe KV cache por usuário). Eles existem aqui só para satisfazer o
  // invariante max_users <= vramSlots que provisionMachine e
  // viableGpuIdsForTemplate aplicam.
  //
  // 18 GB: pesos medidos no HF — transformer 7,75 + text encoder 8,05 + VAE
  // 0,17 = 15,97 GB em bf16, mais margem de ativação.
  //
  // 30 GB "por usuário": faz vramSlots(48, 18, 30) = floor(30/30) = 1. Ou
  // seja, a PRÓPRIA fórmula de capacidade passa a rejeitar uma segunda stack
  // nesta máquina, em vez de isso depender apenas do max_users. Vale porque
  // ficar fora de SHARED_POD_PLANS não torna o pod dedicado — aquela constante
  // só controla DISABLE_PREFIX_CACHING e o piso de vagas reservadas.
  model_footprint_gb: 18,
  kv_reserve_gb_per_user: 30,
  lora_footprint_gb: 0,
  max_users: 1,
  usage_class_config: { max_high: 1 },

  // is_test: permite máquina MANUAL pelo painel, e bloqueia provisionamento
  // automático e alocação de usuário novo (migration 0054). É o estado certo
  // para um template que ainda não tem load test.
  is_enabled: true,
  is_test: true,

  env: {
    // Pesos no volume. O entrypoint avisa no log se esta variável faltar,
    // porque um template sem ela BOOTA normal e o custo só aparece na segunda
    // despausa.
    HF_HOME: "/models/huggingface",

    IMAGE_MODEL_REVISION: MODEL_REVISION,
    IMAGE_SERVED_MODEL_NAME: "flux2-klein-4b",
    IMAGE_DTYPE: "bfloat16",

    // 4 steps e guidance 1.0 porque o checkpoint é distilled
    // (`is_distilled: true` no model_index.json, `guidance_embeds: false` no
    // transformer/config.json).
    IMAGE_STEPS: "4",
    IMAGE_STEPS_MAX: "8",
    IMAGE_GUIDANCE_SCALE: "1.0",
    IMAGE_MAX_SEQUENCE_LENGTH: "512",

    IMAGE_DEFAULT_SIZE: "1024x1024",
    IMAGE_ALLOWED_SIZES: "1024x1024,1536x1024,1024x1536",
    IMAGE_IMAGES_PER_REQUEST_MAX: "1",
    IMAGE_OUTPUT_FORMAT: "png",

    IMAGE_MAX_REFERENCE_IMAGES: "4",
    IMAGE_ALLOWED_FORMATS: "png,jpeg,webp",
    IMAGE_MAX_FILE_SIZE_MB: "15",

    // capacity é TOTAL EM VOO (em execução + esperando), não tamanho de fila.
    //
    // Não existe IMAGE_WORKER_CONCURRENCY: a serialização é ESTRUTURAL (uma
    // única task consumidora em policy.GenerationQueue), não configurável. Uma
    // env com esse nome seria pior que nenhuma — pareceria o botão de
    // paralelismo e não faria nada. Mesmo motivo para IMAGE_PIPELINE não estar
    // aqui: o server importa Flux2KleinPipeline diretamente.
    IMAGE_QUEUE_CAPACITY: "4",
    // 60 e não 120: o edge do RunPod corta em ~100-127 s, então um timeout de
    // 120 nunca chegaria a disparar — o cliente receberia 524 do Cloudflare em
    // vez do nosso 504.
    IMAGE_QUEUE_WAIT_TIMEOUT_S: "60",
    IMAGE_WARMUP_RUNS: "2",
    IMAGE_ALLOW_TF32: "true",

    // Nome ATUAL da variável; PYTORCH_CUDA_ALLOC_CONF hoje é só alias de
    // compatibilidade. Os templates de LLM seguem com o nome antigo — trocar
    // lá é outra mudança.
    PYTORCH_ALLOC_CONF: "expandable_segments:True",

    // Agulha que o /health do agent procura em /proc/*/cmdline. Tem que casar
    // com a linha de comando do docker/image/entrypoint.sh. Sem ela o agent usa
    // o default (a string do vLLM), reporta vllm_alive=false durante todo o
    // boot, e o painel mostra "Falha" num pod que só está baixando os pesos.
    SERVER_PROCESS_MATCH: "/opt/agent/server.py",
  },

  // VLLM_EXTRA_ARGS ausente de propósito: vllmFlagsFromTemplate devolve tudo
  // null (served_model_name, max_model_len, max_concurrent_seqs e
  // max_images_per_prompt), que é o correto para um pod que não é vLLM.
  // parseImageLimit(undefined) devolve null, não 999 — conferido em
  // lib/machines.ts:54.
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

// ---------- 0. idempotência ----------

const { data: existing } = await db
  .from("templates")
  .select("id, name, runpod_template_id")
  .eq("name", NAME)
  .maybeSingle()

if (existing) {
  console.log(`já existe: ${existing.name} (${existing.id}) — nada a fazer`)
  process.exit(0)
}

// ---------- 1. a tag publicada é a que esperamos? ----------
//
// Motivo concreto: a 0.1.0 foi pushada ANTES de uma revisão e ficou no registry
// com bugs (500 em vez de 400 em campo com tipo errado, admissão da fila
// furável por handler cancelado, request pendurada no shutdown). A existência
// da tag não diz nada sobre o conteúdo dela — e o template aponta para a TAG.
//
// Aqui a tag é resolvida no registry e comparada com o digest que foi de fato
// verificado. Se alguém re-pushar a tag, o script recusa em vez de registrar um
// template apontando para conteúdo desconhecido.
const EXPECTED_DIGEST =
  "sha256:66c8f155356b5168ee165d780e19a3ab2fefe2618052819334dfdd9bbe283c5f"

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
      // os dois Accept: buildx publica índice OCI, mas uma imagem construída
      // sem buildx seria manifest v2 — aceitar só um daria 404 enganoso
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

const [repo, tag] = IMAGE.split(":")
let digest
try {
  digest = await resolveTagDigest(repo, tag)
} catch (e) {
  console.error(`\nnão foi possível verificar a imagem no registry: ${e.message}`)
  console.error("Rode com IMAGE_CHECK=skip para seguir sem esta verificação.")
  if (process.env.IMAGE_CHECK !== "skip") process.exit(1)
}

if (process.env.IMAGE_CHECK !== "skip") {
  if (digest === null) {
    console.error(`\nERRO: a tag ${IMAGE} não existe no registry.`)
    console.error("Faça o build+push antes (ver docker/image/README.md).")
    process.exit(1)
  }
  if (digest !== EXPECTED_DIGEST) {
    console.error(`\nERRO: ${IMAGE} não é o artefato verificado.`)
    console.error(`  esperado: ${EXPECTED_DIGEST}`)
    console.error(`  registry: ${digest}`)
    console.error(
      "A tag foi re-pushada. Ou bumpe a versão (a regra é: tag nunca é\n" +
        "re-pushada) ou atualize EXPECTED_DIGEST aqui e no README depois de\n" +
        "verificar o conteúdo novo.",
    )
    process.exit(1)
  }
  console.log("imagem   : digest confere com o artefato verificado")
}

// ---------- 2. pré-checagem do invariante de capacidade ----------
//
// Espelha lib/capacity.ts:vramSlots. Checar AQUI evita criar um template que o
// provisionMachine vai recusar depois — o erro apareceria só na hora de subir a
// máquina, com o template já cadastrado e o registro no RunPod já criado.

const A40_VRAM_GB = 48
const vramSlots = Math.floor(
  Math.max(A40_VRAM_GB * TEMPLATE.gpu_count - TEMPLATE.model_footprint_gb, 0) /
    TEMPLATE.kv_reserve_gb_per_user,
)

console.log("template :", TEMPLATE.name, "→", TEMPLATE.model_name)
console.log("imagem   :", TEMPLATE.image)
console.log("gpu      :", TEMPLATE.gpu_count, "×", TEMPLATE.gpu_types.join(", "))
console.log("disco    :", `container=${TEMPLATE.disk_gb}GB`, `volume=${TEMPLATE.volume_gb}GB em ${TEMPLATE.volume_mount_path}`)
console.log("capac.   :", `max_users=${TEMPLATE.max_users}`, `vramSlots=${vramSlots}`,
  `usage_class_config=${JSON.stringify(TEMPLATE.usage_class_config)}`)
console.log("revision :", MODEL_REVISION)

if (TEMPLATE.max_users > vramSlots) {
  console.error(
    `\nERRO: max_users=${TEMPLATE.max_users} > vramSlots=${vramSlots} — provisionMachine recusaria a criação da máquina.`,
  )
  process.exit(1)
}

// ---------- 3. insere no Supabase ----------
//
// ORDEM INVERTIDA em relação a lib/actions.ts:createTemplate (que cria no
// RunPod primeiro), e de propósito.
//
// A Server Action faz RunPod-primeiro porque o `runpod_template_id` faz parte
// do único insert dela. Aqui o fluxo é nosso, e o RunPod-primeiro tem um modo
// de falha concreto: se o insert falhar — e o mais provável é justamente o
// CHECK de plano, quando a 0057 não rodou — sobra um template ÓRFÃO no console
// do RunPod, que ninguém referencia e que na próxima execução vira um segundo
// órfão (o guard de idempotência olha o Supabase, não o RunPod).
//
// Supabase-primeiro não tem esse problema: se o insert falha, nada foi criado
// em lugar nenhum. E a falha no sentido oposto (RunPod indisponível) já era
// tolerada pelo desenho — o registro local vale sem o espelho, e dá para
// vincular depois via importTemplate.

const { data: inserted, error: insErr } = await db
  .from("templates")
  .insert({ ...TEMPLATE, runpod_template_id: null })
  .select("*")
  .single()

if (insErr) {
  if (/templates_plan_valid|violates check constraint/i.test(insErr.message)) {
    console.error(
      `\ninsert falhou no CHECK de plano: ${insErr.message}\n\n` +
        "Aplique supabase/migrations/0057_plan_image.sql antes de rodar este\n" +
        "script. Nada foi criado — nem no Supabase, nem no RunPod.",
    )
    process.exit(1)
  }
  throw new Error(`insert falhou: ${insErr.message}`)
}

console.log("\nregistro no Supabase criado:", inserted.id)

// ---------- 4. cria no RunPod e vincula ----------

let runpodTemplateId = null
try {
  const created = await runpod("/templates", {
    method: "POST",
    json: {
      name: TEMPLATE.name,
      imageName: TEMPLATE.image,
      containerDiskInGb: TEMPLATE.disk_gb,
      volumeInGb: TEMPLATE.volume_gb,
      volumeMountPath: TEMPLATE.volume_mount_path,
      // MODEL_NAME e AGENT_ADMIN_SECRET NÃO vão aqui: são injetados por
      // máquina no provisionamento (lib/actions.ts:podInputFromTemplate), não
      // pelo template.
      env: TEMPLATE.env,
      ports: [
        ...TEMPLATE.http_ports.map((p) => `${p}/http`),
        ...TEMPLATE.tcp_ports.map((p) => `${p}/tcp`),
      ],
    },
  })
  runpodTemplateId = created.id
  const { error: linkErr } = await db
    .from("templates")
    .update({ runpod_template_id: runpodTemplateId })
    .eq("id", inserted.id)
  if (linkErr) {
    // O template existe nos dois lados mas não está vinculado. Não é fatal —
    // o provisionamento não usa runpod_template_id (podInputFromTemplate passa
    // imageName e env explícitos) —, mas o id vai na mensagem para o vínculo
    // manual, senão ele fica só no console do RunPod.
    console.error(
      `\nAVISO: template criado no RunPod (${runpodTemplateId}) mas o vínculo\n` +
        `falhou: ${linkErr.message}\n` +
        `Vincule à mão: update templates set runpod_template_id = '${runpodTemplateId}' where id = '${inserted.id}';`,
    )
  } else {
    inserted.runpod_template_id = runpodTemplateId
    console.log("RunPod template criado e vinculado:", runpodTemplateId)
  }
} catch (e) {
  console.error(
    `\nfalha ao criar no RunPod: ${e.message}\n` +
      "O registro local vale mesmo sem o espelho no console do RunPod — o\n" +
      "provisionamento passa imageName e env explícitos. Para vincular depois,\n" +
      "use o import de template no painel.",
  )
}

// ---------- 5. verify de leitura de volta ----------
//
// SELECT novo, e não a linha devolvida pelo insert: o vínculo do
// runpod_template_id acontece num update DEPOIS, então a linha do insert está
// desatualizada. E o precedente é o updateTemplate, que engole erro em
// silêncio (lib/actions.ts:383) — a única forma de saber o que o banco tem é
// perguntar ao banco.

const { data: row, error: readErr } = await db
  .from("templates")
  .select("*")
  .eq("id", inserted.id)
  .single()

if (readErr) throw new Error(`leitura de volta falhou: ${readErr.message}`)

const checks = {
  plan: row.plan === "Image",
  image_tag: row.image === IMAGE,
  nao_usa_latest: !row.image.endsWith(":latest"),
  model_name: row.model_name === TEMPLATE.model_name,
  model_revision: row.env.IMAGE_MODEL_REVISION === MODEL_REVISION,
  // as três que, se falharem, o pod sobe e parece saudável mas se comporta mal
  hf_home_no_volume: row.env.HF_HOME?.startsWith(row.volume_mount_path),
  server_process_match: row.env.SERVER_PROCESS_MATCH === "/opt/agent/server.py",
  volume_persistente: Number(row.volume_gb) >= 40,
  // dedicação da máquina
  max_users: row.max_users === 1,
  usage_class_config: JSON.stringify(row.usage_class_config) === JSON.stringify({ max_high: 1 }),
  is_test: row.is_test === true,
  is_enabled: row.is_enabled === true,
  // um VLLM_EXTRA_ARGS aqui indicaria template copiado de um de LLM por engano
  sem_vllm_args: row.env.VLLM_EXTRA_ARGS === undefined,
}

// Vínculo com o RunPod: informativo, não fatal. O provisionamento não usa essa
// coluna (podInputFromTemplate passa imageName e env explícitos), então um
// template sem ela sobe pod normalmente — mas ele não aparece no console do
// RunPod, e é bom saber disso agora em vez de estranhar depois.
if (row.runpod_template_id) {
  console.log("\nvínculo RunPod:", row.runpod_template_id)
} else {
  console.log("\nvínculo RunPod: AUSENTE (não impede subir pod; vincule via import no painel)")
}

console.log("\ntemplate criado:", inserted.id)
for (const [k, ok] of Object.entries(checks)) console.log(`  ${ok ? "ok  " : "FALHA"} ${k}`)

if (Object.values(checks).some((ok) => !ok)) {
  console.error("\nverify falhou — confira o template no painel antes de subir pod")
  process.exit(1)
}

console.log(`
pronto. o pod é você quem sobe:
  /templates → nova máquina manual com o ${NAME} (is_test=true permite manual)

primeiro boot baixa ~16 GB para o volume; acompanhe com
  curl {public_url}/health     → espere {"vllm_ready": true}
`)
