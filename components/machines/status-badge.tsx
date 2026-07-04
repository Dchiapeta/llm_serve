import { Badge } from "@/components/ui/badge"
import type { Machine } from "@/lib/types"

const config: Record<
  Machine["status"],
  { label: string; className: string }
> = {
  creating: { label: "Criando", className: "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300" },
  running: { label: "Rodando", className: "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300" },
  stopped: { label: "Parada", className: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300" },
  terminated: { label: "Apagada", className: "bg-zinc-100 text-zinc-500 dark:bg-zinc-900 dark:text-zinc-400" },
  error: { label: "Erro", className: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300" },
}

export function StatusBadge({ status }: { status: Machine["status"] }) {
  const c = config[status] ?? config.error
  return <Badge className={c.className}>{c.label}</Badge>
}
