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
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { CopyableId } from "@/components/contas/copyable-id"

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
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>ID</TableHead>
                <TableHead>Nome</TableHead>
                <TableHead>E-mail</TableHead>
                <TableHead>Stacks</TableHead>
                <TableHead>Tokens</TableHead>
                <TableHead>Requests</TableHead>
                <TableHead>Criada em</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {accounts.length === 0 && (
                <TableRow>
                  <TableCell
                    colSpan={7}
                    className="text-center text-muted-foreground"
                  >
                    Nenhuma conta ainda.
                  </TableCell>
                </TableRow>
              )}
              {accounts.map((account) => (
                <TableRow key={account.id}>
                  <TableCell>
                    <CopyableId value={account.id} />
                  </TableCell>
                  <TableCell className="text-sm font-medium">
                    {account.name}
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {account.email ?? "—"}
                  </TableCell>
                  <TableCell className="text-sm tabular-nums">
                    {stackCountByAccount.get(account.id) ?? 0}
                  </TableCell>
                  <TableCell className="text-sm tabular-nums">
                    {(usageByAccount.get(account.id)?.tokens ?? 0).toLocaleString(
                      "pt-BR"
                    )}
                  </TableCell>
                  <TableCell className="text-sm tabular-nums">
                    {(
                      usageByAccount.get(account.id)?.requests ?? 0
                    ).toLocaleString("pt-BR")}
                  </TableCell>
                  <TableCell className="text-sm">
                    {new Date(account.created_at).toLocaleDateString("pt-BR")}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  )
}
