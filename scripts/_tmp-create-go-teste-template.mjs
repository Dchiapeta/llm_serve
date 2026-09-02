// Cria o GO-Teste: template único do plano Go que muda de estado entre as fases
// do experimento de desempenho. Nasce no estado da FASE B.
//
// Contexto: o Pro foi otimizado em 02/09/2026 com duas alavancas medidas em
// separado — quantização 4-bit (+71%) e MTP (+21%), acumulando +106% de decode.
// O Go nunca recebeu nenhuma das duas e está travado em ~30 tok/s, que é teto
// de banda da A40: os pesos dominam o custo por step de decode.
//
// Matriz de fases (B, C e D são estados SUCESSIVOS deste mesmo registro):
//
//   A  bf16    off   batch 2048  -> llm-stack-413, já de pé (baseline)
//   B  w4a16   off   batch 2048  <- ESTE ESTADO
//   C  w4a16   MTP2  batch 2048  -> edição + pod recriado
//   D  w4a16   MTP2  batch 4096  -> edição + pod recriado
//
// A ordem começa em B, não em C: é a ordem causal (B->C mede o MTP com uma
// variável só) e é a de menor risco — o w4a16 já foi validado no Pro, enquanto
// o MTP é a variável que lá travou o boot (410/411) e matou o engine com CUDA
// illegal memory access na 2ª request concorrente.
//
// Medição que justifica o w4a16 (headers dos safetensors, peso lido por step de
// decode — torre visual e drafter MTP não entram no decode de texto):
//
//   Qwen/Qwen3.5-9B                        15,87 GB/step -> 43,9 tok/s de teto
//   RedHatAI/Qwen3.5-9B-quantized.w4a16     8,00 GB/step -> 87   tok/s de teto
//   QuantTrio/Qwen3.5-9B-AWQ (descartado)   8,93 GB/step -> 78   tok/s de teto
//
// O w4a16 lê 12% menos bytes que o AWQ (simétrico, sem qzeros), é a mesma
// família compressed-tensors do Pro, tem evals publicados e preserva a cabeça
// MTP em bf16 (o card chama save_mtp_tensors_to_checkpoint de propósito), o que
// é o que torna a fase C possível.
//
// Idempotente: se já existe no Supabase, não faz nada.
//
//   node --env-file=.env scripts/_tmp-create-go-teste-template.mjs

import { createClient } from "@supabase/supabase-js"

const SOURCE_TEMPLATE = "Go_A40_Qwen3.5_64K-TEST"

// baseline da origem — se não bater, o "só uma variável muda" deixa de valer
const BASE_ARGS =
  "--dtype bfloat16 --max-model-len 65536 --gpu-memory-utilization 0.90 " +
  "--kv-cache-dtype fp8 --max-num-seqs 16 --served-model-name go-base " +
  '--enable-prompt-tokens-details --limit-mm-per-prompt {"image":4,"video":0}'

// Sai o --dtype bfloat16: o checkpoint já declara bfloat16 no config.json, e o
// template INT4 do Pro não passa a flag. Não somar variável sobre o
// quantization_config do compressed-tensors.
//
// NÃO entra --max-num-batched-tokens: fica no default do
// UsageContext.OPENAI_API_SERVER (2048 em GPU < 70 GiB, arg_utils.py:2416), que
// é o mesmo da fase A. Subir para 4096 é a fase D, medida em separado.
//
// NÃO entra --speculative-config: é a fase C.
const NOVO_ARGS = BASE_ARGS.replace("--dtype bfloat16 ", "")

const OVERRIDES = {
  name: "GO-Teste",
  model_name: "RedHatAI/Qwen3.5-9B-quantized.w4a16",

  // 10,65 GiB de pesos em disco + margem para CUDA graphs, torre visual (fica
  // em bf16), drafter MTP e buffers. PROVISÓRIO: corrigir com o
  // "Model loading took ... GiB" do log de boot da fase B.
  model_footprint_gb: 13,

  // KV real a 64K = 1,073 GB. São 16 KB/token: só 8 das 32 camadas têm KV
  // (full_attention_interval 4), com 4 kv_heads × head_dim 256 em fp8. A
  // origem tinha 1, que subestimava.
  kv_reserve_gb_per_user: 1.1,

  // Inalterado de propósito. Subir para 28 é uma fase separada, depois de o
  // footprint real estar medido. Invariante: 25 <= floor((48-13)/1.1) = 31.
  max_users: 25,

  // A origem está NULL, o que faz machine_high_cap devolver None e a admissão
  // de high cair no fail-open da migration 0037. 7 é o valor que o template Go
  // anterior carregava, derivado de GPU-segundos de prefill.
  usage_class_config: { max_high: 7 },

  is_test: true,
  is_enabled: true,
}

// Proteção operacional herdada do Pro, que a adotou depois de dois OOMs de boot
// na L40S. É uma variável a mais em A->B (o template de produção não a tem),
// declarada como tal no plano: é flag de alocador, anti-fragmentação, sem
// efeito esperado em throughput.
const ENV_EXTRA = { PYTORCH_CUDA_ALLOC_CONF: "expandable_segments:True" }

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
  env: { ...source.env, ...ENV_EXTRA, VLLM_EXTRA_ARGS: NOVO_ARGS },
}

// invariante de viableGpuIdsForTemplate: max_users > vramSlots quebra a criação
// de máquina. A40 = 48 GB (VRAM crua — a fórmula ignora o
// gpu-memory-utilization 0.90, por isso o dimensionamento do plano saiu da
// conta física e esta checagem serve só de guarda).
const VRAM_A40_GB = 48
const vramSlots = Math.floor(
  Math.max(VRAM_A40_GB - next.model_footprint_gb, 0) / next.kv_reserve_gb_per_user,
)

console.log("origem :", source.name, `(${source.id})`)
console.log("novo   :", next.name, "| fase B: w4a16, sem MTP, batch no default")
console.log("modelo :", next.model_name)
console.log("gpu    :", next.gpu_count, "×", next.gpu_types.join(", "), "| imagem", next.image)
console.log("capac. :", `max_users=${next.max_users}`, `footprint=${next.model_footprint_gb}GB`,
  `kv/user=${next.kv_reserve_gb_per_user}GB`, `usage_class=${JSON.stringify(next.usage_class_config)}`)
console.log("       :", `vramSlots=${vramSlots}`, `→ invariante ${next.max_users} ≤ ${vramSlots}`,
  next.max_users <= vramSlots ? "ok" : "VIOLADO")
console.log("flags  :", `is_test=${next.is_test}`, `is_enabled=${next.is_enabled}`)
console.log("\nargs origem:", BASE_ARGS)
console.log("args novo  :", NOVO_ARGS)

if (next.max_users > vramSlots) {
  console.error("\nABORTADO: max_users acima do que a fórmula de capacidade permite.")
  process.exit(1)
}

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

// ---------- 5. verify de leitura de volta, NOS DOIS LADOS ----------
// updateTemplate/createTemplate já engoliram erro do RunPod em silêncio antes
// (lib/actions.ts:383 só faz console.error) — nunca confiar no write.

let rpEnv = null
if (runpodTemplateId) {
  try {
    const rp = await runpod(`/templates/${runpodTemplateId}`)
    rpEnv = rp.env ?? null
  } catch (e) {
    console.error("falha ao reler o template no RunPod:", e.message)
  }
}

const args = inserted.env.VLLM_EXTRA_ARGS

const checks = {
  // o que muda nesta fase
  "model_name é o w4a16": inserted.model_name === "RedHatAI/Qwen3.5-9B-quantized.w4a16",
  "args sem --dtype": args === NOVO_ARGS && !args.includes("--dtype"),
  "PYTORCH_CUDA_ALLOC_CONF setado":
    inserted.env.PYTORCH_CUDA_ALLOC_CONF === "expandable_segments:True",

  // o que NÃO pode estar aqui (são as fases seguintes)
  "sem --speculative-config (fase C)": !args.includes("speculative-config"),
  "sem --max-num-batched-tokens (fase D)": !args.includes("max-num-batched-tokens"),

  // o que tem que ficar intacto
  "--max-model-len 65536": args.includes("--max-model-len 65536"),
  "--max-num-seqs 16": args.includes("--max-num-seqs 16"),
  "--kv-cache-dtype fp8": args.includes("--kv-cache-dtype fp8"),
  "--gpu-memory-utilization 0.90": args.includes("--gpu-memory-utilization 0.90"),
  "alias go-base": args.includes("--served-model-name go-base"),
  "--enable-prompt-tokens-details": args.includes("--enable-prompt-tokens-details"),
  "--limit-mm-per-prompt (imagens on)": args.includes('--limit-mm-per-prompt {"image":4'),
  "sem --language-model-only": !args.includes("language-model-only"),
  "sem --tensor-parallel-size": !args.includes("tensor-parallel-size"),
  "prefix caching por cache_salt": inserted.env.PREFIX_CACHE_ISOLATION === "cache_salt",
  "reasoning parser qwen3": inserted.env.REASONING_PARSER === "qwen3",
  "tool call parser qwen3_coder": inserted.env.TOOL_CALL_PARSER === "qwen3_coder",
  "LoRA off": inserted.env.ENABLE_LORA === "false",

  // capacidade
  "footprint 13 (provisório)": Number(inserted.model_footprint_gb) === 13,
  "kv_reserve 1.1": Number(inserted.kv_reserve_gb_per_user) === 1.1,
  "max_users 25 (igual à origem)": inserted.max_users === 25 && inserted.max_users === source.max_users,
  "max_high 7 (corrige o fail-open)": inserted.usage_class_config?.max_high === 7,
  "invariante max_users ≤ vramSlots": inserted.max_users <= vramSlots,

  // hardware e roteamento
  "gpu_count 1 igual à origem": inserted.gpu_count === 1 && inserted.gpu_count === source.gpu_count,
  "plan Go": inserted.plan === "Go",
  "is_test true (inerte p/ alocação automática)": inserted.is_test === true,
  "runpod vinculado": !!inserted.runpod_template_id,

  // o outro lado da escrita
  "RunPod relido com os mesmos args": rpEnv ? rpEnv.VLLM_EXTRA_ARGS === NOVO_ARGS : false,
  "RunPod com PYTORCH_CUDA_ALLOC_CONF":
    rpEnv ? rpEnv.PYTORCH_CUDA_ALLOC_CONF === "expandable_segments:True" : false,
}

console.log("\ntemplate criado no Supabase:", inserted.id)
for (const [k, ok] of Object.entries(checks)) console.log(`  ${ok ? "ok   " : "FALHA"} ${k}`)
if (Object.values(checks).some((ok) => !ok)) {
  console.error("\nverify falhou — confira no painel antes de subir pod")
  process.exit(1)
}

console.log(`
pronto — fase B gravada. O pod é você quem sobe.

Ao subir, o que conferir no /admin/logs (NÃO no /health, que responde 200 com o
vLLM morto), capturando nos primeiros ~2 min porque o buffer é circular:

  - a linha "non-default args: {...}" — é ELA, não o template, que registra o
    que foi medido (no Pro o log dizia spec_tokens 5 e o script gravava 3)
  - quantization=compressed-tensors
  - speculative_config AUSENTE/None  (é a fase B)
  - max_model_len=65536, max_num_seqs=16, kv_cache_dtype=fp8
  - enable_prefix_caching=True
  - "Model loading took ... GiB"  -> corrige o model_footprint_gb provisório
  - "GPU KV cache size: N tokens" -> N/65536 = sessões MEDIDAS
  - /v1/models devolvendo "go-base"

E para o tráfego chegar nele (template is_test=true é inerte para a alocação
automática): gravar antes os valores atuais, apontar stacks.machine_id da stack
de teste para a máquina e api_keys.stack_id da chave de teste para essa stack —
e desfazer os dois no fim da fase (machine_id -> NULL), senão a stack fica
presa ao pod experimental.
`)
