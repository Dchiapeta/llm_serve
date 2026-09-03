// Cria o template de TESTE de geração de imagem: FLUX.2 Klein 4B numa A40.
//
// Diferente dos outros _tmp-create-*, este NÃO é copy-with-override: não existe
// template de origem compatível (todos os atuais são vLLM), então os campos vão
// literais. O que se mantém do padrão daqueles scripts é o que importa:
//
//   - RunPod primeiro, Supabase depois (mesma ordem de lib/actions.ts:createTemplate);
//   - insere usage_class_config e is_test, que a Server Action NÃO escreve
//     (usage_class_config NULL = fail-open do machine_high_cap da migration 0037);
//   - verify de leitura de volta nos dois lados, pelo precedente do
//     updateTemplate que engole erro do RunPod em silêncio (lib/actions.ts:383);
//   - idempotente: se o template já existe no Supabase, não faz nada.
//
// PRÉ-REQUISITOS (nesta ordem):
//   1. supabase/migrations/0057_plan_image.sql aplicada — sem ela o CHECK
//      rejeita plan='Image' e o insert falha.
//   2. dchiapeta/diffusers-agent:flux2-klein-4b-0.1.1 publicada — o template
//      aponta para essa tag.
//
//   node --env-file=.env scripts/_tmp-create-image-template.mjs

import { createClient } from "@supabase/supabase-js"

const NAME = "IMAGE-A40-FLUX2-KLEIN-4B"

// Tag versionada e imutável, nunca :latest. Mudança de conteúdo é 0.1.1 — é o
// que torna apontar para a tag equivalente a apontar para o digest, e é a lição
// do template que ficou pinado em :v2 servindo uma imagem pré-tool-calling.
const IMAGE = "dchiapeta/diffusers-agent:flux2-klein-4b-0.1.1"

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

// ---------- 1. pré-checagem do invariante de capacidade ----------
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

// ---------- 2. cria no RunPod ----------

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
  console.log("\nRunPod template criado:", runpodTemplateId)
} catch (e) {
  // Mesmo comportamento da Server Action: o registro local vale mesmo sem o
  // espelho no console do RunPod (dá para vincular depois via importTemplate).
  console.error("\nfalha ao criar no RunPod (seguindo só com o registro local):", e.message)
}

// ---------- 3. insere no Supabase ----------

const { data: inserted, error: insErr } = await db
  .from("templates")
  .insert({ ...TEMPLATE, runpod_template_id: runpodTemplateId })
  .select("*")
  .single()

if (insErr) {
  // O erro mais provável aqui é o CHECK de plano: se a 0057 não rodou,
  // plan='Image' é rejeitado.
  if (/templates_plan_valid|violates check constraint/i.test(insErr.message)) {
    console.error(
      `\ninsert falhou no CHECK de plano: ${insErr.message}\n` +
        "Aplique supabase/migrations/0057_plan_image.sql antes de rodar este script.",
    )
    process.exit(1)
  }
  throw new Error(`insert falhou: ${insErr.message}`)
}

// ---------- 4. verify de leitura de volta ----------

const checks = {
  plan: inserted.plan === "Image",
  image_tag: inserted.image === IMAGE,
  nao_usa_latest: !inserted.image.endsWith(":latest"),
  model_name: inserted.model_name === TEMPLATE.model_name,
  model_revision: inserted.env.IMAGE_MODEL_REVISION === MODEL_REVISION,
  // as três que, se falharem, o pod sobe e parece saudável mas se comporta mal
  hf_home_no_volume: inserted.env.HF_HOME?.startsWith(inserted.volume_mount_path),
  server_process_match: inserted.env.SERVER_PROCESS_MATCH === "/opt/agent/server.py",
  volume_persistente: Number(inserted.volume_gb) >= 40,
  // dedicação da máquina
  max_users: inserted.max_users === 1,
  usage_class_config: JSON.stringify(inserted.usage_class_config) === JSON.stringify({ max_high: 1 }),
  is_test: inserted.is_test === true,
  is_enabled: inserted.is_enabled === true,
  // um VLLM_EXTRA_ARGS aqui indicaria template copiado de um de LLM por engano
  sem_vllm_args: inserted.env.VLLM_EXTRA_ARGS === undefined,
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
