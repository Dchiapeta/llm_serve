export type TemplatePlan = "VibeCoder" | "Pro" | "Max" | "Enterprise"

export const TEMPLATE_PLANS: TemplatePlan[] = [
  "VibeCoder",
  "Pro",
  "Max",
  "Enterprise",
]

// Planos cujo pod é COMPARTILHADO entre tenants (várias stacks/contas no mesmo
// processo vLLM). Espelha SHARED_POD_PLANS do gateway (docker/gateway/main.py).
// Nesses planos o prefix caching vira canal lateral de tempo entre co-tenants
// (ver docker/entrypoint.sh), então o provisionamento força DISABLE_PREFIX_CACHING.
export const SHARED_POD_PLANS: TemplatePlan[] = ["VibeCoder", "Pro"]

export type Template = {
  id: string
  runpod_template_id: string | null
  name: string
  image: string
  model_name: string
  plan: TemplatePlan
  gpu_types: string[]
  gpu_count: number
  env: Record<string, string>
  start_command: string | null
  disk_gb: number
  volume_gb: number
  volume_mount_path: string
  http_ports: string[]
  tcp_ports: string[]
  model_footprint_gb: number
  kv_reserve_gb_per_user: number
  lora_footprint_gb: number
  max_users: number | null
  created_at: string
}

export type Machine = {
  id: string
  runpod_pod_id: string | null
  name: string
  gpu_type: string
  status: "creating" | "running" | "stopped" | "terminated" | "error"
  template_id: string | null
  admin_secret: string
  // path do HF, usado como --model do vLLM no boot (ex.: "Qwen/Qwen3.6-27B-FP8")
  model_name: string | null
  // alias com que o vLLM SERVE (--served-model-name, ex.: "pro-base"); é o que o
  // gateway fixa no campo "model" das requisições. NULL = sem alias → usa model_name.
  served_model_name: string | null
  vram_gb: number | null
  cost_per_hr: number | null
  public_url: string | null
  max_users: number | null
  created_at: string
}

// Account = identidade do cliente, só isso. Toda configuração de produto
// (plano, system_prompt, RAG, fine-tune, máquina) vive em Stack — uma conta
// pode ter várias stacks com configurações diferentes (migration 0027).
export type Account = {
  id: string
  name: string
  email: string | null
  user_id: string | null
  created_at: string
}

// Stack = um produto/LLM contratado por uma conta. Uma conta pode ter várias.
// slug é o subdomínio de acesso do cliente ao manager (roteamento por
// subdomínio ainda não implementado).
export type Stack = {
  id: string
  account_id: string
  machine_id: string | null
  plan: TemplatePlan
  purchase_date: string // date "YYYY-MM-DD"
  slug: string
  // Override de system prompt da stack; null = sem override.
  system_prompt: string | null
  created_at: string
}

// Chunk de texto da base de conhecimento (RAG) de uma stack, com embedding
// já calculado (OpenAI text-embedding-3-small, 1536 dimensões).
export type KnowledgeChunk = {
  id: string
  account_id: string
  // null nos chunks legados de contas que tinham 2+ stacks no momento da
  // migration 0020 (não dava para saber a qual stack pertenciam).
  stack_id: string | null
  storage_path: string
  chunk_index: number
  content: string
  created_at: string
}

export type ApiKey = {
  id: string
  account_id: string
  machine_id: string
  stack_id: string | null
  key_hash: string
  key_prefix: string
  // Chave em texto puro, para cópia posterior pelo painel. Chaves criadas
  // antes da migration 0014 ficam null (só o prefixo é recuperável).
  plain_key: string | null
  status: "active" | "revoked"
  created_at: string
}

// Adapter LoRA de uma conta, já treinado e armazenado no Supabase Storage
// (bucket "loras", prefixo {account_id}/{version}/).
export type LoraAdapter = {
  id: string
  account_id: string
  // Stack dona do adapter (migration 0026). Null nos adapters registrados
  // antes da migration numa conta com 2+ stacks — ambíguo, sem como saber
  // qual stack é a dona; fica invisível ao roteamento até um admin reassociar.
  stack_id: string | null
  storage_path: string
  version: string
  status: "ready" | "invalid"
  created_at: string
}

// Estado de roteamento de uma conta: onde está (ou deveria estar) seu adapter.
// Durante migração (lora_status = "migrating"), machine_id continua apontando
// para a origem, que segue servindo até o flip pós-load no destino.
export type RoutingState = {
  // Escopo por STACK desde a migration 0029 (era por conta). stack_id é a PK;
  // account_id continua denormalizado para histórico/joins.
  stack_id: string
  account_id: string
  machine_id: string | null
  lora_adapter_id: string | null
  lora_status: "unloaded" | "loading" | "loaded" | "migrating"
  last_used_at: string | null
  updated_at: string
}

// Log de alocação/migração de máquina por conta — routing_state só guarda
// o estado atual, este é o histórico (um registro por evento).
export type RoutingHistory = {
  id: string
  account_id: string
  machine_id: string | null
  from_machine_id: string | null
  lora_adapter_id: string | null
  event: "allocated" | "migrated" | "released"
  created_at: string
}

export type UsageMetric = {
  id: string
  api_key_id: string | null
  machine_id: string
  window_start: string
  requests: number
  tokens_in: number
  tokens_out: number
  concurrent_peak: number
}

export type MachineEvent = {
  id: string
  machine_id: string | null
  type: string
  message: string
  created_at: string
}
