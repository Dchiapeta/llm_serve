import { Badge } from "@/components/ui/badge"
import type { MachineDisplayStatus } from "@/lib/machines"

// As cores usam o modificador `!` porque os estilos da ReUI
// (.style-nova .cn-badge-variant-*) têm especificidade maior que utilitários.
const config: Record<
  MachineDisplayStatus,
  { label: string; className: string; dot?: "pulse" | "solid" }
> = {
  creating: {
    label: "Criando",
    className:
      "bg-blue-100! text-blue-700! dark:bg-blue-950! dark:text-blue-300!",
    dot: "pulse",
  },
  starting: {
    label: "Subindo",
    className: "bg-sky-100! text-sky-700! dark:bg-sky-950! dark:text-sky-300!",
    dot: "pulse",
  },
  running: {
    label: "Rodando",
    className:
      "bg-emerald-100! text-emerald-700! dark:bg-emerald-950! dark:text-emerald-300!",
    dot: "solid",
  },
  stopped: {
    label: "Parada",
    className:
      "bg-amber-100! text-amber-700! dark:bg-amber-950! dark:text-amber-300!",
  },
  terminated: {
    label: "Apagada",
    className:
      "bg-zinc-100! text-zinc-500! dark:bg-zinc-900! dark:text-zinc-400!",
  },
  error: {
    label: "Erro",
    className: "bg-red-100! text-red-700! dark:bg-red-950! dark:text-red-300!",
  },
}

export function StatusBadge({ status }: { status: MachineDisplayStatus }) {
  const c = config[status] ?? config.error
  return (
    <Badge className={c.className}>
      {c.dot && (
        <span
          className={`mr-1 size-1.5 rounded-full bg-current ${
            c.dot === "pulse" ? "animate-pulse" : ""
          }`}
        />
      )}
      {c.label}
    </Badge>
  )
}
