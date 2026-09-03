import { Badge } from "@/components/ui/badge"
import type { TemplatePlan } from "@/lib/types"

// As cores usam o modificador `!` porque os estilos da ReUI
// (.style-nova .cn-badge-variant-*) têm especificidade maior que utilitários.
const config: Record<TemplatePlan, string> = {
  Go:
    "bg-teal-100! text-teal-700! dark:bg-teal-950! dark:text-teal-300!",
  Pro: "bg-indigo-100! text-indigo-700! dark:bg-indigo-950! dark:text-indigo-300!",
  Max: "bg-purple-100! text-purple-700! dark:bg-purple-950! dark:text-purple-300!",
  Enterprise:
    "bg-amber-100! text-amber-700! dark:bg-amber-950! dark:text-amber-300!",
  // rose: fora da faixa teal→indigo→purple→amber usada pela escada de LLM,
  // porque Image não é um degrau dela e sim outra linha de produto.
  Image: "bg-rose-100! text-rose-700! dark:bg-rose-950! dark:text-rose-300!",
}

export function PlanBadge({ plan }: { plan: TemplatePlan | undefined }) {
  if (!plan) {
    return <span className="text-muted-foreground">—</span>
  }
  return <Badge className={config[plan]}>{plan}</Badge>
}
