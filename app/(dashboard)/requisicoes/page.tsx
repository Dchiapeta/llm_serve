import { createSupabaseAdmin } from "@/lib/supabase/server"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { RequestsTable, type RequestRow } from "@/components/dashboard/requests-table"

export const dynamic = "force-dynamic"

const REQUEST_LIMIT = 200

export default async function RequisicoesPage() {
  const db = createSupabaseAdmin()

  const { data: requestsData } = await db
    .from("gateway_requests")
    .select("*, stacks(slug), api_keys(key_prefix), accounts(name)")
    .order("created_at", { ascending: false })
    .limit(REQUEST_LIMIT)

  const requests = (requestsData ?? []) as RequestRow[]

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold">Requisições</h1>
        <p className="text-sm text-muted-foreground">
          Chamadas feitas ao gateway, atreladas a stack e chave
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Histórico</CardTitle>
          <CardDescription>
            Últimas {REQUEST_LIMIT} requisições
          </CardDescription>
        </CardHeader>
        <CardContent>
          <RequestsTable rows={requests} />
        </CardContent>
      </Card>
    </div>
  )
}
