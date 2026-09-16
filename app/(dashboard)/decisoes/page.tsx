import { createSupabaseAdmin } from "@/lib/supabase/server"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { DecisionsTable, type DecisionRow } from "@/components/dashboard/decisions-table"

export const dynamic = "force-dynamic"

const DECISION_LIMIT = 200

// Espelho de /requisicoes para provision_decisions (migration 0070): o que o
// gateway decidiu sobre criar/religar/recriar máquina — inclusive o que NÃO
// fez e os 503 servidos sem criar nada, que não aparecem em gateway_requests.
export default async function DecisoesPage() {
  const db = createSupabaseAdmin()

  const { data } = await db
    .from("provision_decisions")
    .select("*, stacks(slug, plan), api_keys(key_prefix), accounts(name), machines(name)")
    .order("created_at", { ascending: false })
    .limit(DECISION_LIMIT)

  const rows = (data ?? []) as DecisionRow[]

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold">Decisões de máquina</h1>
        <p className="text-sm text-muted-foreground">
          Por que o gateway criou, religou ou recriou uma máquina — e por que não.
          Linhas repetidas na mesma janela de 30 s são agrupadas (coluna Repetições).
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Histórico</CardTitle>
          <CardDescription>Últimas {DECISION_LIMIT} decisões · retenção de 30 dias</CardDescription>
        </CardHeader>
        <CardContent>
          <DecisionsTable rows={rows} />
        </CardContent>
      </Card>
    </div>
  )
}
