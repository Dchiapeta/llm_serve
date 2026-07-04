export type Template = {
  id: string
  runpod_template_id: string | null
  name: string
  image: string
  model_name: string
  gpu_types: string[]
  env: Record<string, string>
  disk_gb: number
  model_footprint_gb: number
  kv_reserve_gb_per_user: number
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
