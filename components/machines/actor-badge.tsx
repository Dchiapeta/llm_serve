import { Badge } from "@/components/ui/badge"
import {
  ACTOR_KIND_LABELS,
  actorKind,
  actorLabel,
  type ActorKind,
} from "@/components/machines/cause-labels"

// Manual × Requisição × Automática — o primeiro corte de qualquer evento do
// ciclo de vida. Sufixo `!` pelo mesmo motivo de plan-badge.tsx.
const KIND_CLASS: Record<ActorKind, string> = {
  manual: "bg-indigo-100! text-indigo-700! dark:bg-indigo-950! dark:text-indigo-300!",
  request: "bg-teal-100! text-teal-700! dark:bg-teal-950! dark:text-teal-300!",
  automatic: "bg-zinc-100! text-zinc-700! dark:bg-zinc-900! dark:text-zinc-300!",
}

export function ActorBadge({
  actor,
  email,
}: {
  actor: string | null | undefined
  // quem clicou, quando for manual com sessão — vai no próprio rótulo
  email?: string | null
}) {
  const kind = actorKind(actor)
  if (!kind) return <span className="text-muted-foreground">—</span>
  const label =
    kind === "manual" && email ? `${ACTOR_KIND_LABELS.manual} · ${email}` : ACTOR_KIND_LABELS[kind]
  return (
    // ator cru no tooltip (admin/panel/request/lifecycle/gateway)
    <Badge className={KIND_CLASS[kind]} title={actorLabel(actor)}>
      {label}
    </Badge>
  )
}
