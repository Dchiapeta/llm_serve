// Rótulos pt-BR do vocabulário fechado de `cause` (migration 0070). Fonte
// única no painel — o slug é gravado pelo gateway (trigger_ctx.py) e pelas
// server actions (lib/actions.ts); o texto vive só aqui.
//
// Formato do slug: <verbo>[_denied].<gatilho>.<motivo>. Um slug desconhecido
// (gateway mais novo que o painel) cai no fallback legível de `causeLabel`.

const LABELS: Record<string, string> = {
  // provisionamento concedido
  "provision.request.no_free_slot": "Requisição de cliente sem máquina com vaga",
  "provision.request.no_base_machine": "Requisição de cliente sem máquina do modelo base",
  "provision.pool.refill": "Reposição proativa do pool (watermark)",
  "provision.rebalance.high_caps": "Rebalanceamento de stacks de uso alto",
  "provision.panel.manual": "Criada manualmente no painel",
  "provision.panel.stack_create": "Criada ao cadastrar uma stack",
  "provision.stack_migration": "Criada para receber uma stack migrada",
  "provision.gateway.unknown": "Pedida pelo gateway (sem originador)",
  // provisionamento negado
  "provision_denied.switch_off": "Negado: provisionamento automático desligado",
  "provision_denied.panel_unconfigured": "Negado: painel não configurado no gateway",
  "provision_denied.lock_active": "Negado: já há uma criação em andamento",
  "provision_denied.cooldown": "Negado: tentativa recente (cooldown)",
  "provision_denied.panel_error": "Negado: o painel recusou ou falhou",
  // decisões gravadas pelo próprio painel (recordDecision em lib/actions.ts)
  "provision_denied.runpod_error": "Negado: o RunPod recusou criar o pod",
  "provision_denied.validation": "Negado: teto de usuários acima da GPU",
  "provision_denied.template_blocked": "Negado: template de teste/desabilitado",
  // wake
  "wake.request.no_machine_available": "Religada: requisição sem máquina disponível",
  "wake.request.stack_home_paused": "Religada: máquina da stack estava pausada",
  "wake_denied.cooldown": "Wake negado: tentativa recente (cooldown)",
  "wake_denied.no_gpu": "Wake negado: host sem GPU livre",
  "wake_denied.failed": "Wake negado: falha no startPod",
  // recriação
  "recreate.request.no_gpu_on_wake": "Recriada: host sem GPU ao religar",
  "recreate.request.stack_home_no_gpu": "Recriada: host da stack sem GPU",
  "recreate.request.pod_lost": "Recriada: pod sumiu do RunPod",
  "recreate.lifecycle.pending_retry": "Recriada: retry do lifecycle",
  "recreate.panel.manual": "Recriada manualmente no painel",
  "recreate.gateway.unknown": "Recriada a pedido do gateway (sem originador)",
  "recreate_denied.lock_active": "Recriação negada: já em andamento",
  "recreate_denied.cooldown": "Recriação negada: tentativa recente",
  "recreate_denied.panel_unconfigured": "Recriação negada: painel não configurado",
  "recreate_denied.panel_error": "Recriação negada: o painel recusou ou falhou",
  "recreate_denied.template_blocked": "Recriação negada: template de teste/desabilitado",
  "recreate_denied.runpod_error": "Recriação negada: o RunPod recusou",
  "recreate_denied.validation": "Recriação negada: teto de usuários acima da GPU",
  // parada / partida
  "stop.provision.pause_when_healthy": "Pausada ao ficar saudável (reserva do pool)",
  "stop.lifecycle.idle": "Auto-pausa por ociosidade",
  "stop.panel.manual": "Pausada manualmente no painel",
  "start.panel.manual": "Iniciada manualmente no painel",
  "terminate.panel.manual": "Apagada manualmente no painel",
  // stacks
  "stack.reallocated": "Stack realocada (origem indisponível)",
  "stack.placed": "Stack re-alocada após ociosidade",
  "stack.rebalanced": "Stack movida por balanceamento",
  "stack.rebalance_pending": "Stack de uso alto aguardando vaga",
  "stack.released_idle": "Stack liberada por ociosidade",
  "stack.consolidated": "Contas consolidadas em outra máquina",
  "stack.migrated_manual": "Stack migrada manualmente",
  // chaves
  "key.created": "Chave criada",
  "key.revoked": "Chave revogada",
  // 503 pré-rota, nada criado
  "denied_503.waking": "503: máquina religando",
  "denied_503.provisioning": "503: máquina sendo criada",
  "denied_503.recreating": "503: máquina sendo recriada",
  "denied_503.preparing": "503: nada a fazer (preparando)",
  "denied_503.agent_starting": "503: agent do pod iniciando",
  "denied_503.capacity": "503: sem capacidade no plano",
}

export function causeLabel(cause: string | null | undefined): string {
  if (!cause) return "—"
  return LABELS[cause] ?? cause
}

// Família do slug — decide a cor do badge e o agrupamento na UI.
export type CauseFamily =
  | "provision"
  | "wake"
  | "recreate"
  | "stop"
  | "start"
  | "terminate"
  | "stack"
  | "key"
  | "denied"
  | "served_503"
  | "other"

export function causeFamily(cause: string | null | undefined): CauseFamily {
  if (!cause) return "other"
  if (cause.startsWith("denied_503.")) return "served_503"
  if (cause.includes("_denied.")) return "denied"
  const head = cause.split(".")[0]
  switch (head) {
    case "provision":
    case "wake":
    case "recreate":
    case "stop":
    case "start":
    case "terminate":
    case "stack":
    case "key":
      return head
    default:
      return "other"
  }
}

// Só as causas que fazem uma máquina EXISTIR ou VOLTAR: é o que o card "Por
// que esta máquina subiu" procura no histórico.
export function isBirthCause(cause: string | null | undefined): boolean {
  const f = causeFamily(cause)
  return f === "provision" || f === "recreate" || f === "wake" || f === "start"
}

export const ACTOR_LABELS: Record<string, string> = {
  request: "requisição de cliente",
  lifecycle: "lifecycle (cron do gateway)",
  admin: "admin no painel",
  panel: "painel (server-to-server)",
  gateway: "gateway",
}

export function actorLabel(actor: string | null | undefined): string {
  if (!actor) return "—"
  return ACTOR_LABELS[actor] ?? actor
}

// A pergunta que o operador faz primeiro: foi alguém clicando, foi um
// cliente mandando request, ou o sistema decidiu sozinho? Os cinco `actor`
// gravados colapsam nestes três — é o eixo dos badges e do filtro da UI.
export type ActorKind = "manual" | "request" | "automatic"

export function actorKind(actor: string | null | undefined): ActorKind | null {
  switch (actor) {
    case "admin":
    case "panel":
      return "manual"
    case "request":
      return "request"
    case "lifecycle":
    case "gateway":
      return "automatic"
    default:
      return null
  }
}

export const ACTOR_KIND_LABELS: Record<ActorKind, string> = {
  manual: "Manual",
  request: "Requisição",
  automatic: "Automática",
}
