// Cria o templete-teste-2: laboratório para isolar UMA variável de desempenho.
//
// Estratégia: copy-with-override do templete_teste (baseline validado em
// 02/09/2026 — 64,6 tok/s single-stream, estável até 10 concorrentes). Tudo
// que não está em OVERRIDES é copiado literalmente, inclusive as env NCCL_*.
//
// A ÚNICA variável que muda: sai o --disable-custom-all-reduce. Medição do
// projeto: o all-reduce por SHM custa ~9,6 ms dos ~31,9 ms por token (~128
// coletivas/token a 75 µs); por IPC seria ~20 µs. Se boa parte dos 15,5
// ms/token atuais for isso, o teto vai pra ~90-100 tok/s.
//
// O risco conhecido é que essa flag é parte do fix do deadlock de boot em
// TP=2 no RunPod (PCIe sem NVLink). Ela nunca foi testada sozinha: no boot
// que travou, o custom all-reduce estava ativo JUNTO com 51 CUDA graphs e
// spec-tokens 5. É exatamente essa confusão que este template desfaz.
//
// Idempotente: se já existe no Supabase, não faz nada.
//
//   node --env-file=.env scripts/_tmp-create-teste2-template.mjs

import { createClient } from "@supabase/supabase-js"

const SOURCE_TEMPLATE = "templete_teste"

// baseline, para derivar o override e provar que só uma coisa muda
const BASE_ARGS =
  '--max-model-len 32768 --gpu-memory-utilization 0.90 --kv-cache-dtype fp8 ' +
  '--max-num-seqs 16 --disable-custom-all-reduce ' +
  '--speculative-config {"method":"mtp","num_speculative_tokens":3} ' +
  '--served-model-name pro-base'

const NOVO_ARGS = BASE_ARGS.replace("--disable-custom-all-reduce ", "")

const OVERRIDES = {
  name: "templete-teste-2",
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

// ---------- 1. lê a origem ----------

const { data: source, error: srcErr } = await db
  .from("templates").select("*").eq("name", SOURCE_TEMPLATE).single()
if (srcErr) throw new Error(`template de origem não encontrado: ${srcErr.message}`)

// trava de segurança: se a origem não for o baseline esperado, o "só uma
// variável muda" deixa de ser verdade e o teste perde o sentido
if (source.env.VLLM_EXTRA_ARGS !== BASE_ARGS) {
  console.error("ABORTADO: a origem não está no baseline esperado.")
  console.error("  esperado:", BASE_ARGS)
  console.error("  atual   :", source.env.VLLM_EXTRA_ARGS)
  process.exit(1)
}

const { data: existing } = await db
  .from("templates").select("id, name").eq("name", OVERRIDES.name).maybeSingle()
if (existing) {
  console.log(`já existe: ${existing.name} (${existing.id}) — nada a fazer`)
  process.exit(0)
}

// ---------- 2. monta o novo ----------

const { id: _id, runpod_template_id: _rp, created_at: _ts, ...copied } = source
const next = {
  ...copied,
  ...OVERRIDES,
  env: { ...source.env, VLLM_EXTRA_ARGS: NOVO_ARGS },
}

console.log("origem :", source.name)
console.log("novo   :", next.name)
console.log("modelo :", next.model_name, "|", next.gpu_count, "×", next.gpu_types.join(", "))
console.log("capac. :", `max_users=${next.max_users}`, `footprint=${next.model_footprint_gb}GB`,
  `kv/user=${next.kv_reserve_gb_per_user}GB`, `usage_class=${JSON.stringify(next.usage_class_config)}`)
console.log("\nargs antes:", BASE_ARGS)
console.log("args depois:", NOVO_ARGS)

// ---------- 3. RunPod ----------

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
  console.error("\nfalha ao criar no RunPod (seguindo só com o registro local):", e.message)
}

// ---------- 4. Supabase ----------

const { data: inserted, error: insErr } = await db
  .from("templates").insert({ ...next, runpod_template_id: runpodTemplateId }).select("*").single()
if (insErr) throw new Error(`insert falhou: ${insErr.message}`)

// ---------- 5. verify de leitura de volta ----------
// (o updateTemplate já engoliu erro em silêncio antes — nunca confiar no write)

const checks = {
  "args sem custom-all-reduce": inserted.env.VLLM_EXTRA_ARGS === NOVO_ARGS,
  "nada de --disable-custom-all-reduce": !inserted.env.VLLM_EXTRA_ARGS.includes("disable-custom-all-reduce"),
  "spec-tokens 3 preservado": inserted.env.VLLM_EXTRA_ARGS.includes('"num_speculative_tokens":3'),
  "max-num-seqs 16 preservado": inserted.env.VLLM_EXTRA_ARGS.includes("--max-num-seqs 16"),
  "sem --tensor-parallel-size": !inserted.env.VLLM_EXTRA_ARGS.includes("tensor-parallel-size"),
  "NCCL_P2P_DISABLE=1": inserted.env.NCCL_P2P_DISABLE === "1",
  "reasoning parser": inserted.env.REASONING_PARSER === "qwen3",
  "gpu_count igual": inserted.gpu_count === source.gpu_count,
  "max_users igual": inserted.max_users === source.max_users,
  "usage_class_config igual":
    JSON.stringify(inserted.usage_class_config) === JSON.stringify(source.usage_class_config),
  "runpod vinculado": !!inserted.runpod_template_id,
}

console.log("\ntemplate criado:", inserted.id)
for (const [k, ok] of Object.entries(checks)) console.log(`  ${ok ? "ok   " : "FALHA"} ${k}`)
if (Object.values(checks).some((ok) => !ok)) {
  console.error("\nverify falhou — confira no painel antes de subir pod")
  process.exit(1)
}
console.log("\npronto. o pod é você quem sobe.")
