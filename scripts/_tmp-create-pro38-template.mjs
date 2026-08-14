// Cria o template de TESTE do Pro com Qwen3.8-27B-FP8.
//
// Estratégia: copy-with-override do template Pro em produção
// (Pro_2xA40_Qwen3.6-27B_128K). Tudo que não está em OVERRIDES é copiado
// literalmente do template atual — inclusive as env NCCL_*, que são o que
// impede o deadlock de TP=2 no RunPod (PCIe sem NVLink).
//
// Só UMA variável muda de fato: model_name. É isso que torna o load test
// comparável contra o baseline do Pro.
//
// Replica o caminho de lib/actions.ts:createTemplate (RunPod primeiro, depois
// Supabase), com dois cuidados que a Server Action não tem:
//   - usage_class_config: createTemplate NÃO insere essa coluna; aqui ela vem
//     copiada do template de origem no mesmo insert, senão max_high ficaria
//     NULL = sem teto (fail-open da 0037) e o template de teste admitiria
//     heavy sem limite.
//   - verify de leitura de volta, pelo precedente do updateTemplate que engole
//     erro silenciosamente (docs/claude-code.md).
//
// Idempotente: se o template já existe no Supabase, não faz nada.
//
//   node --env-file=.env scripts/_tmp-create-pro38-template.mjs

import { createClient } from "@supabase/supabase-js"

const SOURCE_TEMPLATE = "Pro_2xA40_Qwen3.6-27B_128K"

const OVERRIDES = {
  name: "Pro_2xA40_Qwen3.8-27B_128K-TEST",
  // FP8, não o bf16: na A40 (CC 8.6) os pesos bf16 dobram a leitura por token
  // em decode (27 GB/GPU vs 13,5), cortando o teto de ~50 pra ~26 tok/s. O
  // gargalo do 2xA40 já é decode, não VRAM.
  model_name: "Qwen/Qwen3.8-27B-FP8",
}

// Mantidos IGUAIS ao Pro de propósito, apesar do modelo novo:
//   model_footprint_gb 28   — mesmo tamanho, mesma quantização fp8 block-128
//   kv_reserve_gb_per_user  — arquitetura idêntica (16 layers full, 4 KV heads,
//                             head_dim 256) => 32 KiB/token, e 131072 × 32 KiB
//                             = 4,19 GB. O orçamento (96−28)/4,3 continua dando
//                             15 usuários.
//   usage_class_config      — max_high 4 é teto de COMPUTE, e é o número que o
//                             load test tem que revalidar (o 3.8 pensa mais).
//   VLLM_EXTRA_ARGS         — inclusive --served-model-name pro-base: mesmo
//                             alias do Pro, então a config do cliente não muda
//                             pra apontar no teste (precedente do Go-TEST).

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
  .eq("name", OVERRIDES.name)
  .maybeSingle()

if (existing) {
  console.log(`já existe: ${existing.name} (${existing.id}) — nada a fazer`)
  process.exit(0)
}

// ---------- 2. monta o novo ----------

const { id: _id, runpod_template_id: _rp, created_at: _ts, ...copied } = source
const next = { ...copied, ...OVERRIDES }

console.log("origem :", source.name, "→", source.model_name)
console.log("novo   :", next.name, "→", next.model_name)
console.log("gpu    :", next.gpu_count, "×", next.gpu_types.join(", "))
console.log("capac. :", `max_users=${next.max_users}`, `footprint=${next.model_footprint_gb}GB`,
  `kv/user=${next.kv_reserve_gb_per_user}GB`, `usage_class_config=${JSON.stringify(next.usage_class_config)}`)
console.log("args   :", next.env.VLLM_EXTRA_ARGS)

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
      // máquina no provisionamento (lib/actions.ts:420), não pelo template.
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

const checks = {
  model_name: inserted.model_name === OVERRIDES.model_name,
  max_users: inserted.max_users === source.max_users,
  model_footprint_gb: Number(inserted.model_footprint_gb) === Number(source.model_footprint_gb),
  kv_reserve_gb_per_user:
    Number(inserted.kv_reserve_gb_per_user) === Number(source.kv_reserve_gb_per_user),
  usage_class_config:
    JSON.stringify(inserted.usage_class_config) === JSON.stringify(source.usage_class_config),
  vllm_args: inserted.env.VLLM_EXTRA_ARGS === source.env.VLLM_EXTRA_ARGS,
  nccl_p2p_disable: inserted.env.NCCL_P2P_DISABLE === "1",
}

console.log("\ntemplate criado:", inserted.id)
for (const [k, ok] of Object.entries(checks)) console.log(`  ${ok ? "ok  " : "FALHA"} ${k}`)

if (Object.values(checks).some((ok) => !ok)) {
  console.error("\nverify falhou — confira o template no painel antes de subir pod")
  process.exit(1)
}
console.log("\npronto. o pod é você quem sobe.")
