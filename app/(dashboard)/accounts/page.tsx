import Link from "next/link"

import { createSupabaseAdmin } from "@/lib/supabase/server"
import type { Account, ApiKey, Machine } from "@/lib/types"
import { Badge } from "@/components/ui/badge"
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
import { CreateAccountDialog } from "@/components/accounts/create-account-dialog"
import { CreateKeyDialog } from "@/components/accounts/create-key-dialog"
import { RevokeKeyButton } from "@/components/accounts/revoke-key-button"

export const dynamic = "force-dynamic"

type KeyRow = ApiKey & {
  accounts: { name: string } | null
  machines: { name: string; id: string } | null
}

export default async function AccountsPage() {
  const db = createSupabaseAdmin()

  const [{ data: accountsData }, { data: machinesData }, { data: keysData }] =
    await Promise.all([
      db.from("accounts").select("*").order("name"),
      db.from("machines").select("*").in("status", ["running", "stopped", "creating"]),
      db
        .from("api_keys")
        .select("*, accounts(name), machines(id, name)")
        .order("created_at", { ascending: false }),
    ])

  const accounts = (accountsData ?? []) as Account[]
  const machines = (machinesData ?? []) as Machine[]
  const keys = (keysData ?? []) as KeyRow[]

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Contas & Chaves</h1>
          <p className="text-sm text-muted-foreground">
            Usuários das LLMs e suas chaves de acesso HEX
          </p>
        </div>
        <div className="flex gap-2">
          <CreateKeyDialog accounts={accounts} machines={machines} />
          <CreateAccountDialog />
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-[320px_1fr]">
        <Card>
          <CardHeader>
            <CardTitle>Contas</CardTitle>
            <CardDescription>{accounts.length} conta(s)</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {accounts.length === 0 && (
              <p className="text-sm text-muted-foreground">Nenhuma conta ainda.</p>
            )}
            {accounts.map((a) => {
              const activeCount = keys.filter(
                (k) => k.account_id === a.id && k.status === "active"
              ).length
              return (
                <div
                  key={a.id}
                  className="flex items-center justify-between rounded-lg border p-3"
                >
                  <div>
                    <p className="text-sm font-medium">{a.name}</p>
                    {a.email && (
                      <p className="text-xs text-muted-foreground">{a.email}</p>
                    )}
                  </div>
                  <Badge variant="secondary">{activeCount} chave(s)</Badge>
                </div>
              )
            })}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Chaves de acesso</CardTitle>
            <CardDescription>{keys.length} chave(s) no total</CardDescription>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Conta</TableHead>
                  <TableHead>Máquina</TableHead>
                  <TableHead>Chave</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Criada em</TableHead>
                  <TableHead className="w-24" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {keys.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={6} className="text-center text-muted-foreground">
                      Nenhuma chave gerada.
                    </TableCell>
                  </TableRow>
                )}
                {keys.map((k) => (
                  <TableRow key={k.id}>
                    <TableCell className="font-medium">{k.accounts?.name ?? "—"}</TableCell>
                    <TableCell>
                      {k.machines ? (
                        <Link
                          href={`/machines/${k.machines.id}`}
                          className="text-sm hover:underline"
                        >
                          {k.machines.name}
                        </Link>
                      ) : (
                        "—"
                      )}
                    </TableCell>
                    <TableCell className="font-mono text-xs">{k.key_prefix}…</TableCell>
                    <TableCell>
                      {k.status === "active" ? (
                        <Badge variant="secondary">ativa</Badge>
                      ) : (
                        <Badge variant="outline">revogada</Badge>
                      )}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {new Date(k.created_at).toLocaleDateString("pt-BR")}
                    </TableCell>
                    <TableCell>
                      {k.status === "active" && (
                        <RevokeKeyButton keyId={k.id} keyPrefix={k.key_prefix} />
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
