import Link from "next/link"

import { createSupabaseAdmin } from "@/lib/supabase/server"
import type { Account, ApiKey, LoraAdapter, Machine, Stack } from "@/lib/types"
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
import { CreateKeyDialog } from "@/components/accounts/create-key-dialog"
import { RegisterLoraDialog } from "@/components/accounts/register-lora-dialog"
import { RevokeKeyButton } from "@/components/accounts/revoke-key-button"

export const dynamic = "force-dynamic"

type KeyRow = ApiKey & {
  accounts: { name: string } | null
  machines: { name: string; id: string } | null
}

type LoraRow = LoraAdapter & {
  accounts: { name: string } | null
  stacks: { slug: string } | null
}

type StackOption = Stack & { accounts: { name: string } | null }

export default async function AccountsPage() {
  const db = createSupabaseAdmin()

  const [
    { data: accountsData },
    { data: machinesData },
    { data: keysData },
    { data: lorasData },
    { data: stacksData },
  ] = await Promise.all([
    db.from("accounts").select("*").order("name"),
    db.from("machines").select("*").in("status", ["running", "stopped", "creating"]),
    db
      .from("api_keys")
      .select("*, accounts(name), machines(id, name)")
      .order("created_at", { ascending: false }),
    db
      .from("lora_adapters")
      .select("*, accounts(name), stacks(slug)")
      .order("created_at", { ascending: false }),
    db.from("stacks").select("*, accounts(name)").order("created_at", { ascending: false }),
  ])

  const accounts = (accountsData ?? []) as Account[]
  const machines = (machinesData ?? []) as Machine[]
  const keys = (keysData ?? []) as KeyRow[]
  const loras = (lorasData ?? []) as LoraRow[]
  const stacks = (stacksData ?? []) as StackOption[]

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Chaves</h1>
          <p className="text-sm text-muted-foreground">
            Chaves de acesso HEX e adapters LoRA das contas
          </p>
        </div>
        <div className="flex gap-2">
          <RegisterLoraDialog stacks={stacks} />
          <CreateKeyDialog accounts={accounts} machines={machines} />
        </div>
      </div>

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

      <Card>
        <CardHeader>
          <CardTitle>Adapters LoRA</CardTitle>
          <CardDescription>
            {loras.length} adapter(s) registrado(s) no bucket <code>loras</code>
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Conta</TableHead>
                <TableHead>Stack</TableHead>
                <TableHead>Versão</TableHead>
                <TableHead>Path no storage</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Registrado em</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {loras.length === 0 && (
                <TableRow>
                  <TableCell colSpan={6} className="text-center text-muted-foreground">
                    Nenhum adapter registrado.
                  </TableCell>
                </TableRow>
              )}
              {loras.map((l) => (
                <TableRow key={l.id}>
                  <TableCell className="font-medium">{l.accounts?.name ?? "—"}</TableCell>
                  <TableCell>
                    {l.stacks?.slug ?? (
                      <span className="text-muted-foreground">sem stack</span>
                    )}
                  </TableCell>
                  <TableCell>{l.version}</TableCell>
                  <TableCell className="font-mono text-xs">loras/{l.storage_path}</TableCell>
                  <TableCell>
                    {l.status === "ready" ? (
                      <Badge variant="secondary">pronto</Badge>
                    ) : (
                      <Badge variant="destructive">inválido</Badge>
                    )}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {new Date(l.created_at).toLocaleDateString("pt-BR")}
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
