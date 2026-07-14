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

  const [{ data: accountsData }, { data: stacksData }] = await Promise.all([
    db.from("accounts").select("*").order("created_at", { ascending: false }),
    db.from("stacks").select("id, account_id"),
  ])

  const accounts = (accountsData ?? []) as Account[]
  const stacks = (stacksData ?? []) as Pick<Stack, "id" | "account_id">[]

  const stackCountByAccount = new Map<string, number>()
  for (const s of stacks) {
    stackCountByAccount.set(
      s.account_id,
      (stackCountByAccount.get(s.account_id) ?? 0) + 1
    )
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
                <TableHead>Criada em</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {accounts.length === 0 && (
                <TableRow>
                  <TableCell
                    colSpan={5}
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
