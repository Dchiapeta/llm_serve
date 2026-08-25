export type TemplatePlan = "Go" | "Pro" | "Max" | "Enterprise"

export const TEMPLATE_PLANS: TemplatePlan[] = [
  "Go",
  "Pro",
  "Max",
  "Enterprise",
]

// Estado de cobrança da stack (migration 0050). Espelha o status da
// assinatura na Chargefy, projetado por trigger. 'suspended' e 'canceled'
// cortam o acesso na hora; 'past_due' só corta depois de BILLING_GRACE_HOURS
// contadas de stacks.past_due_since (docker/gateway/main.py:billing_blocked).
export type BillingStatus =
  | "active"
  | "trialing"
  | "past_due"
  | "suspended"
  | "canceled"

// Tolerância entre a assinatura entrar em atraso e a chave parar de
// responder. Espelha BILLING_GRACE_HOURS do gateway, que é quem de fato
// aplica o corte — aqui serve só pra UI mostrar quanto tempo resta.
//
// ATENÇÃO: no gateway o valor é sobrescrevível por env var
// (docker/gateway/main.py). Se ela for configurada em produção sem mudar este
// número, a coluna "Cobrança" do painel passa a mentir sobre a única data que
// o suporte usa pra decidir se cobra ou espera — mude os dois juntos.
export const BILLING_GRACE_HOURS = 72

// Planos que NÃO podem usar a stack para codar: CLI e assistente de código
// (Claude Code, Codex, Cursor, Cline, Roo, Continue) são do Pro para cima.
// Espelha CLI_BLOCKED_PLANS de docker/gateway/cli_policy.py, que é quem de fato
// corta (403). Aqui serve para o painel não ENSINAR uma config que o gateway
// recusa — mesmo cuidado de BILLING_GRACE_HOURS e MAX_CLIENTS_BY_PLAN abaixo.
export const CLI_BLOCKED_PLANS: TemplatePlan[] = ["Go"]

// Planos cujo pod é COMPARTILHADO entre tenants (várias stacks/contas no mesmo
// processo vLLM). Espelha SHARED_POD_PLANS do gateway (docker/gateway/main.py).
// Nesses planos o prefix caching vira canal lateral de tempo entre co-tenants
// (ver docker/entrypoint.sh), então o provisionamento força DISABLE_PREFIX_CACHING.
export const SHARED_POD_PLANS: TemplatePlan[] = ["Go", "Pro"]

// Quantidade máxima de arquivos indexados na base de conhecimento (RAG) por
// stack, por plano. null = sem limite. Consumido pela Server Action de upload
// (lib/actions.ts) e pela UI (knowledge-files-dialog.tsx), que precisa do
// mesmo valor pra mostrar contagem/estado de "cheio" sem round-trip.
export const RAG_FILE_LIMIT_BY_PLAN: Record<TemplatePlan, number | null> = {
  Go: 5,
  Pro: 20,
  Max: 50,
  Enterprise: null,
}

// Quantos LUGARES o plano pode conectar. São duas constantes porque são duas
// camadas com garantias diferentes:
//
//   MAX_KEYS_BY_PLAN    — o CONTRATO. Chaves ativas (purpose 'customer') por
//     stack, aplicado na emissão (assertKeyQuota em lib/actions.ts). Exato e
//     sem falso positivo: ou a chave existe, ou não.
//   MAX_CLIENTS_BY_PLAN — o DETECTOR de quem furou o contrato usando UMA
//     chave em todo canto. Conta ambientes distintos (ferramenta + bloco de
//     rede) vistos nos últimos CLIENT_WINDOW_DAYS, e é APROXIMADO por
//     construção — ver o docstring de docker/gateway/client_identity.py.
//
// null = sem limite, mesma convenção de RAG_FILE_LIMIT_BY_PLAN.
export const MAX_KEYS_BY_PLAN: Record<TemplatePlan, number | null> = {
  Go: 3,
  Pro: 25,
  Max: 50,
  Enterprise: null,
}

// ATENÇÃO: espelha MAX_CLIENTS_BY_PLAN de docker/gateway/client_identity.py,
// que é quem de fato aplica o teto e aceita override por env
// (MAX_CLIENTS_GO/PRO/MAX). Divergir faz a UI mentir sobre o número que corta
// o cliente — mesmo cuidado de BILLING_GRACE_HOURS acima.
export const MAX_CLIENTS_BY_PLAN: Record<TemplatePlan, number | null> = {
  Go: 5,
  Pro: 25,
  Max: 50,
  Enterprise: null,
}

// Janela deslizante da vaga de ambiente: um lugar sem uso há mais que isso
// libera a vaga sozinho. Espelha CLIENT_WINDOW_DAYS do gateway. É o que evita
// que trocar de notebook vire ticket de suporte.
export const CLIENT_WINDOW_DAYS = 14

// Teto de tamanho por arquivo de RAG, igual pra todos os planos. embedTexts
// (lib/actions.ts) faz batching em lotes de 500 chunks por chamada à API de
// embeddings — 10MB de texto puro dá ~10 mil chunks (~20 chamadas), ainda
// tranquilo em tempo de processamento por upload.
export const MAX_KNOWLEDGE_FILE_SIZE_BYTES = 10 * 1024 * 1024

export type Template = {
  id: string
  runpod_template_id: string | null
  name: string
  // Disponibilidade operacional (migration 0054). Desabilitado não cria
  // máquinas; teste permite máquina manual, mas nunca recebe usuário novo.
  is_enabled: boolean
  is_test: boolean
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
  // Override dos pesos/limiares da classificação de consumo (migration 0032):
  // {weights: {low, medium, high}, daily_medium, daily_high,
  //  req_pct_medium, req_pct_high}. Null = defaults do código.
  usage_class_config: Record<string, unknown> | null
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
  // janela de contexto do vLLM (--max-model-len); o gateway usa para clampar
  // max_tokens ao orçamento restante. NULL = template sem a flag → sem clamp.
  max_model_len: number | null
  // teto de sequências concorrentes do vLLM (--max-num-seqs); o gateway aplica
  // o 429 por máquina a partir daqui (check_concurrency). NULL = template sem a
  // flag → o gateway usa o próprio fallback (DEFAULT_MAX_CONCURRENT_SEQS).
  max_concurrent_seqs: number | null
  // teto de imagens por prompt do vLLM (--limit-mm-per-prompt). O gateway
  // recorta imagem acima disso e avisa no prompt, em vez de deixar o pod
  // devolver 400 — que num cliente que reenvia a conversa toda envenenaria a
  // sessão inteira. NULL = desconhecido → o gateway não recorta nada.
  max_images_per_prompt: number | null
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
  // Coluna do TryStac (supabase/SHARED_SCHEMA.md), não deste repo — hoje
  // sempre igual ao slug; existe porque o TryStac dá grant de UPDATE nela
  // pro usuário final renomear a stack.
  name: string
  // Override de system prompt da stack; null = sem override.
  system_prompt: string | null
  // Defaults de sampling aplicados pelo gateway quando o cliente não manda o
  // parâmetro na requisição (temperature/top_p: migration 0035;
  // max_tokens/presence_penalty: migration 0056). Null = passthrough puro,
  // sem default.
  default_temperature: number | null
  default_top_p: number | null
  default_max_tokens: number | null
  default_presence_penalty: number | null
  // Classe de consumo (migration 0032), derivada do uso real pelo loop do
  // gateway; pesa na ocupação de máquina (low=1.0, medium=1.5, high=3.0).
  usage_class: "low" | "medium" | "high"
  usage_class_updated_at: string | null
  // Estado de cobrança (migration 0050). Escrito pelo trigger que projeta
  // chargefy_subscriptions (repo TryStac) e pelo loop de reconciliação do
  // gateway — nunca pela aplicação. 'past_due' ainda tem acesso: é o par com
  // past_due_since, contra BILLING_GRACE_HOURS, que corta.
  billing_status: BillingStatus
  past_due_since: string | null
  // Idempotência do provisionamento por checkout (= client_reference de
  // chargefy_checkout_attempts). Null em stack criada pelo painel admin.
  provisioning_ref: string | null
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

// "customer" conta pro slot de capacidade e pra cota diária de tokens;
// "playground" é a chave interna gerada junto com a stack (migration 0044),
// nunca exibida ao cliente, isenta dos dois.
export type ApiKeyPurpose = "customer" | "playground"

export type ApiKey = {
  id: string
  account_id: string
  // Pin histórico da máquina, não rota: o gateway resolve por
  // stacks.machine_id. Null enquanto a stack ainda não foi homeada — a chave
  // de Playground nasce assim e o gateway preenche no primeiro request
  // (place_base_stack → rebind_stack_keys).
  machine_id: string | null
  stack_id: string | null
  key_hash: string
  key_prefix: string
  // Chave em texto puro, para cópia posterior pelo painel. Chaves criadas
  // antes da migration 0014 ficam null (só o prefixo é recuperável).
  plain_key: string | null
  status: "active" | "revoked"
  purpose: ApiKeyPurpose
  created_at: string
  expires_at: string | null
  // Tocado pelo coletor periódico de usage_metrics do gateway (migration
  // 0046) — granularidade de METRICS_COLLECTION_INTERVAL_S, não por request.
  last_used_at: string | null
}

// Um LUGAR de onde a stack é usada (migration 0051). Escrito pelo gateway via
// RPC touch_stack_client, uma linha por ambiente admitido — nunca por request
// (o gateway só fala com o banco quando vê um fingerprint novo ou vencido).
export type StackClient = {
  id: string
  stack_id: string
  account_id: string | null
  // Última chave vista neste ambiente, não a única: o mesmo lugar pode rodar
  // Claude Code com uma chave e um script com outra.
  api_key_id: string | null
  // sha256(versão|ferramenta|bloco de rede) — nunca reversível para um IP.
  fingerprint: string
  client_label: string | null
  user_agent: string | null
  // Bloco /24 (IPv4) ou /48 (IPv6). Nunca o endereço completo: o bloco basta
  // para o dono da stack reconhecer o próprio ambiente e é bem mais
  // defensável que guardar dado pessoal identificável.
  ip_bucket: string | null
  first_seen_at: string
  last_seen_at: string
  requests: number
  // 'released' = a vaga foi liberada no painel. A linha fica, porque o
  // histórico é o que distingue troca de notebook de compartilhamento.
  status: "active" | "released"
  released_at: string | null
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

// Uma linha por requisição individual completada pelo gateway (migration
// 0038) — diferente de UsageMetric, que é agregado por janela de ~2min.
// Gravado fire-and-forget no fechamento de cada requisição; só cobre as que
// passaram de autenticação e resolução de rota (mesmo escopo do in_flight
// do gateway), então rejeições precoces (401/429/503) não aparecem aqui.
export type GatewayRequest = {
  id: string
  account_id: string | null
  stack_id: string | null
  api_key_id: string | null
  machine_id: string | null
  path: string
  model: string | null
  // User-Agent cru do cliente (migration 0041), classificado em rótulo de
  // origem na UI por lib/request-origin.ts. NULL em requisições anteriores à
  // migration ou em cliente sem o header — aí o fallback é o path.
  user_agent: string | null
  status_code: number
  stream: boolean
  tokens_in: number | null
  tokens_out: number | null
  duration_ms: number | null
  created_at: string
}

export type MachineEvent = {
  id: string
  machine_id: string | null
  type: string
  message: string
  created_at: string
}
