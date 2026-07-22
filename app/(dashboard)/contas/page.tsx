import { createSupabaseAdmin } from "@/lib/supabase/server"
import type { Account, Stack } from "@/lib/types"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  UsuariosTable,
  type UsuarioRow,
} from "@/components/contas/usuarios-table"

export const dynamic = "force-dynamic"

export default async function ContasPage() {
  const db = createSupabaseAdmin()

  const [
    { data: accountsData },
    { data: stacksData },
    { data: keysData },
    { data: usageData },
  ] = await Promise.all([
    db.from("accounts").select("*").order("created_at", { ascending: false }),
    db.from("stacks").select("id, account_id"),
    db.from("api_keys").select("id, account_id"),
    db.from("usage_metrics").select("api_key_id, tokens_in, tokens_out, requests"),
  ])

  const accounts = (accountsData ?? []) as Account[]
  const stacks = (stacksData ?? []) as Pick<Stack, "id" | "account_id">[]
  const keys = (keysData ?? []) as { id: string; account_id: string }[]
  const usage = (usageData ?? []) as {
    api_key_id: string | null
    tokens_in: number
    tokens_out: number
    requests: number
  }[]

  const stackCountByAccount = new Map<string, number>()
  for (const s of stacks) {
    stackCountByAccount.set(
      s.account_id,
      (stackCountByAccount.get(s.account_id) ?? 0) + 1
    )
  }

  const accountByKeyId = new Map<string, string>()
  for (const k of keys) accountByKeyId.set(k.id, k.account_id)

  const usageByAccount = new Map<
    string,
    { tokens: number; requests: number }
  >()
  for (const u of usage) {
    if (!u.api_key_id) continue
    const accountId = accountByKeyId.get(u.api_key_id)
    if (!accountId) continue
    const agg = usageByAccount.get(accountId) ?? { tokens: 0, requests: 0 }
    agg.tokens += u.tokens_in + u.tokens_out
    agg.requests += u.requests
    usageByAccount.set(accountId, agg)
  }

  const rows: UsuarioRow[] = accounts.map((account) => ({
    id: account.id,
    name: account.name,
    email: account.email ?? null,
    stacks: stackCountByAccount.get(account.id) ?? 0,
    tokens: usageByAccount.get(account.id)?.tokens ?? 0,
    requests: usageByAccount.get(account.id)?.requests ?? 0,
    createdAt: account.created_at,
  }))

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold">Contas</h1>
        <p className="text-sm text-muted-foreground">
          Todos os e-mails cadastrados na base
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Contas</CardTitle>
          <CardDescription>{accounts.length} conta(s)</CardDescription>
        </CardHeader>
        <CardContent>
          <UsuariosTable rows={rows} />
        </CardContent>
      </Card>
    </div>
  )
}
