import { Badge } from "@/components/ui/badge"
import { causeFamily, causeLabel, type CauseFamily } from "@/components/machines/cause-labels"

// Sufixo `!` pelo mesmo motivo de plan-badge.tsx: os estilos da ReUI têm
// especificidade maior que utilitários.
const FAMILY_CLASS: Record<CauseFamily, string> = {
  provision: "bg-emerald-100! text-emerald-700! dark:bg-emerald-950! dark:text-emerald-300!",
  start: "bg-emerald-100! text-emerald-700! dark:bg-emerald-950! dark:text-emerald-300!",
  wake: "bg-sky-100! text-sky-700! dark:bg-sky-950! dark:text-sky-300!",
  recreate: "bg-amber-100! text-amber-700! dark:bg-amber-950! dark:text-amber-300!",
  stop: "bg-zinc-100! text-zinc-700! dark:bg-zinc-900! dark:text-zinc-300!",
  terminate: "bg-red-100! text-red-700! dark:bg-red-950! dark:text-red-300!",
  stack: "bg-violet-100! text-violet-700! dark:bg-violet-950! dark:text-violet-300!",
  key: "bg-zinc-100! text-zinc-700! dark:bg-zinc-900! dark:text-zinc-300!",
  denied: "bg-rose-100! text-rose-700! dark:bg-rose-950! dark:text-rose-300!",
  served_503: "bg-orange-100! text-orange-700! dark:bg-orange-950! dark:text-orange-300!",
  other: "bg-zinc-100! text-zinc-700! dark:bg-zinc-900! dark:text-zinc-300!",
}

export function CauseBadge({ cause }: { cause: string | null | undefined }) {
  if (!cause) return <span className="text-muted-foreground">—</span>
  return (
    // slug cru no tooltip: é o que se procura no código e no SQL
    <Badge className={FAMILY_CLASS[causeFamily(cause)]} title={cause}>
      {causeLabel(cause)}
    </Badge>
  )
}
