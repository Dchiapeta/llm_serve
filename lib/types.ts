export type Template = {
  id: string
  runpod_template_id: string | null
  name: string
  image: string
  model_name: string
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
  model_name: string | null
  vram_gb: number | null
  cost_per_hr: number | null
  public_url: string | null
  max_users: number | null
  created_at: string
}

export type Account = {
  id: string
  name: string
  email: string | null
  created_at: string
}

export type ApiKey = {
  id: string
  account_id: string
  machine_id: string
  key_hash: string
  key_prefix: string
  status: "active" | "revoked"
  created_at: string
}

// Adapter LoRA de uma conta, já treinado e armazenado no Supabase Storage
// (bucket "loras", prefixo {account_id}/{version}/).
export type LoraAdapter = {
  id: string
  account_id: string
  storage_path: string
  version: string
  status: "ready" | "invalid"
  created_at: string
}

// Estado de roteamento de uma conta: onde está (ou deveria estar) seu adapter.
// Durante migração (lora_status = "migrating"), machine_id continua apontando
// para a origem, que segue servindo até o flip pós-load no destino.
export type RoutingState = {
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
