// Cria o template de TESTE do Pro para tráfego agêntico (Claude Code).
//
// Estratégia: copy-with-override do template Pro em produção (PRO-2xA40),
// mesmo padrão de _tmp-create-pro38-template.mjs. Tudo que não está em
// OVERRIDES é copiado literalmente — inclusive as env NCCL_*, que são o que
// impede o deadlock de TP=2 no RunPod (PCIe sem NVLink), os parsers de
// reasoning/tool-call e o PREFIX_CACHE_ISOLATION=cache_salt.
//
// Só VLLM_EXTRA_ARGS muda, e muda em quatro pontos (plano
// ~/.claude/plans/o-que-vc-sugere-atomic-truffle.md, Workstream C):
//   - REMOVE --speculative-config (MTP off): MTP + prefix caching + modelo GDN
//     (Qwen3.5/3.6/3.8) dá `CUDA illegal memory access` — bug ABERTO no vLLM
//     (PR #50021 sem merge, não cobre a race de async scheduling);
//     num_speculative_tokens=3 não protege. Um engine que morre derruba os
//     15 tenants do pod.
//   - ADICIONA --max-num-batched-tokens 16384: default é 2048 no 0.24.0, o que
//     fatia um prefill de 70k em 35 passadas, cada uma pagando all-reduce em
//     PCIe. É a única alavanca do plano que ataca o prefill direto.
//   - ADICIONA --language-model-only: o checkpoint Qwen3_5ForConditionalGeneration
//     carrega a vision tower (bf16 no INT4); em uso de texto isso é VRAM que
//     volta pro pool de KV.
//   - ADICIONA --default-chat-template-kwargs {"reasoning_effort":"low"}: o
//     template do Qwen3.8 abre em xhigh, e em agentic coding xhigh é PIOR que
//     low (raciocina até esgotar max_tokens e devolve vazio — o que mata a
//     compactação do Claude Code). Request-level sobrescreve.
//
// O JSON dos flags NÃO pode ter espaço: o entrypoint expande ${VLLM_EXTRA_ARGS}
// sem aspas e o shell faz word-split (mesmo motivo de o --speculative-config
// atual ser escrito sem espaços).
//
// is_test=true explícito: getDefaultTemplateForPlan filtra is_test=false, então
// este template nunca vira default do plano; o tráfego chega a ele fixando
// stacks.machine_id (padrão rotear-trafego-template-is-test).
//
// Replica o caminho de lib/actions.ts:createTemplate (RunPod primeiro, depois
// Supabase), com os mesmos dois cuidados do pro38: usage_class_config copiado
// no insert (senão max_high fica NULL = sem teto) e verify de leitura de volta.
//
// Idempotente: se o template já existe no Supabase, não faz nada.
//
//   node --env-file=.env scripts/_tmp-create-pro-agentic-template.mjs

import { createClient } from "@supabase/supabase-js"

const SOURCE_TEMPLATE = "PRO-2xA40"
const NEW_NAME = "PRO-2xA40-AGENTIC-TEST"

const ADDED_FLAGS = [
  "--max-num-batched-tokens 16384",
  "--language-model-only",
  '--default-chat-template-kwargs {"reasoning_effort":"low"}',
]

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

// Deriva os args novos a partir dos de produção em vez de reescrever a string
// inteira: assim qualquer flag que a produção ganhe depois continua copiado.
function deriveVllmArgs(sourceArgs) {
  const tokens = sourceArgs.trim().split(/\s+/)
  const out = []
  for (let i = 0; i < tokens.length; i++) {
    if (tokens[i] === "--speculative-config") {
      i += 1 // pula também o JSON que o segue
      continue
    }
    out.push(tokens[i])
  }
  if (out.includes("--speculative-config")) throw new Error("--speculative-config sobrou")

  const served = out.indexOf("--served-model-name")
  const insertAt = served === -1 ? out.length : served
  out.splice(insertAt, 0, ...ADDED_FLAGS)
  return out.join(" ")
}

// ---------- 1. lê o template de origem ----------

const { data: source, error: srcErr } = await db
  .from("templates")
  .select("*")
  .eq("name", SOURCE_TEMPLATE)
  .single()

if (srcErr) throw new Error(`template de origem não encontrado: ${srcErr.message}`)

const { data: existing } = await db
  .from("templates")
  .select("id, name, runpod_template_id")
  .eq("name", NEW_NAME)
  .maybeSingle()

if (existing) {
  console.log(`já existe: ${existing.name} (${existing.id}) — nada a fazer`)
  process.exit(0)
}

// ---------- 2. monta o novo ----------

const { id: _id, runpod_template_id: _rp, created_at: _ts, ...copied } = source
const vllmArgs = deriveVllmArgs(source.env.VLLM_EXTRA_ARGS)
const next = {
  ...copied,
  name: NEW_NAME,
  is_test: true,
  env: { ...source.env, VLLM_EXTRA_ARGS: vllmArgs },
}

console.log("origem :", source.name, "→", source.model_name)
console.log("novo   :", next.name, "→", next.model_name, "(is_test=true)")
console.log("gpu    :", next.gpu_count, "×", next.gpu_types.join(", "))
console.log("capac. :", `max_users=${next.max_users}`, `footprint=${next.model_footprint_gb}GB`,
  `kv/user=${next.kv_reserve_gb_per_user}GB`, `usage_class_config=${JSON.stringify(next.usage_class_config)}`)
console.log("args antes:", source.env.VLLM_EXTRA_ARGS)
console.log("args depois:", next.env.VLLM_EXTRA_ARGS)

// ---------- 3. cria no RunPod ----------

let runpodTemplateId = null
try {
  const created = await runpod("/templates", {
    method: "POST",
    json: {
      name: next.name,
      imageName: next.image,
      containerDiskInGb: next.disk_gb,
      volumeInGb: next.volume_gb,
      volumeMountPath: next.volume_mount_path,
      // MODEL_NAME e AGENT_ADMIN_SECRET NÃO vão aqui: são injetados por
      // máquina no provisionamento (lib/actions.ts), não pelo template.
      env: next.env,
      ports: [
        ...(next.http_ports ?? []).map((p) => `${p}/http`),
        ...(next.tcp_ports ?? []).map((p) => `${p}/tcp`),
      ],
      ...(next.start_command ? { dockerStartCmd: next.start_command.split(/\s+/) } : {}),
    },
  })
  runpodTemplateId = created.id
  console.log("\nRunPod template criado:", runpodTemplateId)
} catch (e) {
  // mesmo comportamento da Server Action: o registro local vale mesmo sem o
  // espelho no console do RunPod (dá pra vincular depois via importTemplate).
  console.error("\nfalha ao criar no RunPod (seguindo só com o registro local):", e.message)
}

// ---------- 4. insere no Supabase ----------

const { data: inserted, error: insErr } = await db
  .from("templates")
  .insert({ ...next, runpod_template_id: runpodTemplateId })
  .select("*")
  .single()

if (insErr) throw new Error(`insert falhou: ${insErr.message}`)

// ---------- 5. verify de leitura de volta ----------

const args = inserted.env?.VLLM_EXTRA_ARGS ?? ""
const checks = {
  is_test: inserted.is_test === true,
  model_name: inserted.model_name === source.model_name,
  max_users: inserted.max_users === source.max_users,
  model_footprint_gb: Number(inserted.model_footprint_gb) === Number(source.model_footprint_gb),
  kv_reserve_gb_per_user:
    Number(inserted.kv_reserve_gb_per_user) === Number(source.kv_reserve_gb_per_user),
  usage_class_config:
    JSON.stringify(inserted.usage_class_config) === JSON.stringify(source.usage_class_config),
  sem_speculative_config: !args.includes("speculative-config"),
  batched_tokens_16384: args.includes("--max-num-batched-tokens 16384"),
  language_model_only: args.includes("--language-model-only"),
  reasoning_effort_low: args.includes('--default-chat-template-kwargs {"reasoning_effort":"low"}'),
  served_model_name_pro_base: args.includes("--served-model-name pro-base"),
  nccl_p2p_disable: inserted.env.NCCL_P2P_DISABLE === "1",
  nccl_ib_disable: inserted.env.NCCL_IB_DISABLE === "1",
  reasoning_parser: inserted.env.ENABLE_REASONING_PARSER === "true" && inserted.env.REASONING_PARSER === "qwen3",
  prefix_cache_isolation: inserted.env.PREFIX_CACHE_ISOLATION === "cache_salt",
  runpod_vinculado: !!inserted.runpod_template_id,
}

console.log("\ntemplate criado:", inserted.id)
for (const [k, ok] of Object.entries(checks)) console.log(`  ${ok ? "ok  " : "FALHA"} ${k}`)

if (Object.values(checks).some((ok) => !ok)) {
  console.error("\nverify falhou — confira o template no painel antes de subir pod")
  process.exit(1)
}
console.log("\npronto. o pod é você quem sobe.")
