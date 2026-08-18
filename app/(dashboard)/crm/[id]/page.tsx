import { Suspense } from "react"
import Link from "next/link"
import { notFound } from "next/navigation"
import { ArrowLeft } from "lucide-react"

import { formatUsd } from "@/lib/billing"
import { BILLING_BADGE, graceRemaining } from "@/lib/billing-status"
import { formatMoney } from "@/lib/chargefy-format"
import { formatDate, formatRelativeDay, formatTokens } from "@/lib/crm"
import { createSupabaseAdmin } from "@/lib/supabase/server"
import { Badge } from "@/components/reui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { CopyableId } from "@/components/contas/copyable-id"

import { getCrmData, parsePeriod, CRM_PERIOD_LABELS } from "../queries"
import { BillingPanel } from "./billing-panel"

export const dynamic = "force-dynamic"

export default async function CrmDetailPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>
  searchParams: Promise<{ period?: string }>
}) {
  const { id } = await params
  const period = parsePeriod((await searchParams).period)

  const { rows } = await getCrmData(period)
  const row = rows.find((r) => r.accountId === id)
  if (!row) notFound()

  const db = createSupabaseAdmin()
  const [
    { data: keysData, error: keysError },
    { data: clientsData, error: clientsError },
  ] = await Promise.all([
    db
      .from("api_keys")
      .select("id, key_prefix, name, purpose, status, last_used_at, created_at, expires_at, stack_id")
      .eq("account_id", id)
      .order("created_at", { ascending: false }),
    db
      .from("stack_clients")
      .select("stack_id, client_label, user_agent, ip_bucket, first_seen_at, last_seen_at, requests, status")
      .eq("account_id", id)
      .order("last_seen_at", { ascending: false }),
  ])

  const keys = (keysData ?? []) as {
    id: string
    key_prefix: string
    name: string | null
    purpose: string
    status: string
    last_used_at: string | null
    created_at: string
    expires_at: string | null
    stack_id: string | null
  }[]
  const clients = (clientsData ?? []) as {
    stack_id: string
    client_label: string | null
    user_agent: string | null
    ip_bucket: string | null
    first_seen_at: string
    last_seen_at: string
    requests: number
    status: string
  }[]

  const badge = row.billingStatus ? BILLING_BADGE[row.billingStatus] : null
  const grace = graceRemaining(row.billingStatus, row.pastDueSince)

  const kpis = [
    {
      label: "Mensal",
      value: row.monthlyNetCents ? formatMoney(row.monthlyNetCents, row.currency) : "—",
    },
    { label: "Tokens", value: formatTokens(row.tokens) },
    { label: "Requisições", value: row.requests.toLocaleString("pt-BR") },
    { label: "Último uso", value: formatRelativeDay(row.lastUsedAt) },
    { label: "Chaves ativas", value: String(row.activeKeys) },
    { label: "Custo GPU", value: formatUsd(row.gpuCostUsd) },
  ]

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-3">
        <Button variant="ghost" size="sm" className="w-fit -ml-2" asChild>
          <Link href="/crm">
            <ArrowLeft className="size-3.5" />
            CRM
          </Link>
        </Button>

        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold">{row.name}</h1>
          {row.plans.map((p) => (
            <Badge key={p} variant="secondary">
              {p}
            </Badge>
          ))}
          {badge && <Badge variant={badge.variant}>{badge.label}</Badge>}
          {grace && (
            <span className="text-xs text-muted-foreground">{grace}</span>
          )}
          {row.kind === "interno" && <Badge variant="outline">Conta interna</Badge>}
        </div>

        <div className="flex flex-wrap items-center gap-4 text-sm text-muted-foreground">
          <span>{row.email ?? "sem e-mail"}</span>
          <CopyableId value={row.accountId} />
          {row.chargefyCustomerId && (
            <span className="font-mono text-xs">
              {row.chargefyCustomerId}
              {row.matchSource === "email" && (
                <span className="ml-1 text-muted-foreground">
                  (associado por e-mail — confirmar)
                </span>
              )}
            </span>
          )}
        </div>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-6">
        {kpis.map((kpi) => (
          <Card key={kpi.label}>
            <CardHeader className="pb-2">
              <CardDescription>{kpi.label}</CardDescription>
            </CardHeader>
            <CardContent>
              <p className="text-2xl font-semibold tracking-tight">{kpi.value}</p>
            </CardContent>
          </Card>
        ))}
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Assinatura</CardTitle>
          <CardDescription>Dados ao vivo da Chargefy</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4 text-sm sm:grid-cols-2 lg:grid-cols-4">
          <Field label="Status">
            {row.subscriptionStatuses.join(", ") || "sem assinatura"}
          </Field>
          <Field label="Preço de tabela">
            {row.monthlyGrossCents
              ? formatMoney(row.monthlyGrossCents, row.currency)
              : "—"}
          </Field>
          <Field label="Cobrado por mês">
            {row.monthlyNetCents
              ? formatMoney(row.monthlyNetCents, row.currency)
              : "—"}
            {row.hasDiscount && (
              <span className="ml-2 text-xs text-muted-foreground">com cupom</span>
            )}
          </Field>
          <Field label="Próxima cobrança">
            {formatDate(row.nextBillingAt)}
            {row.cancelAtPeriodEnd && (
              <span className="ml-2 text-xs text-muted-foreground">
                cancela no fim do ciclo
              </span>
            )}
          </Field>
          <Field label="Cliente desde">{formatDate(row.customerSince)}</Field>
          <Field label="Margem estimada">
            {row.marginBrlCents === null
              ? "—"
              : formatMoney(row.marginBrlCents, "BRL")}
          </Field>
          {row.subscriptionDivergence && (
            <Field label="Divergência">
              <span className="text-destructive">
                banco: {row.billingStatus} · Chargefy:{" "}
                {row.subscriptionStatuses.join(", ")}
              </span>
            </Field>
          )}
          {row.orphanSubscriptions > 0 && (
            <Field label="Atenção">
              <span className="text-destructive">
                {row.orphanSubscriptions} assinatura(s) sem stack provisionada
              </span>
            </Field>
          )}
        </CardContent>
      </Card>

      {row.chargefyCustomerId && (
        <Suspense fallback={<InvoicesSkeleton />}>
          <BillingPanel customerId={row.chargefyCustomerId} />
        </Suspense>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Stacks</CardTitle>
          <CardDescription>
            {row.stacks.length} stack(s) · uso de {CRM_PERIOD_LABELS[period]}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Slug</TableHead>
                <TableHead>Plano</TableHead>
                <TableHead>Máquina</TableHead>
                <TableHead>Cobrança</TableHead>
                <TableHead>Classe</TableHead>
                <TableHead>Último uso</TableHead>
                <TableHead className="text-right!">Tokens</TableHead>
                <TableHead className="text-right!">Requests</TableHead>
                <TableHead className="text-right!">Custo GPU</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {row.stacks.length === 0 && (
                <TableRow>
                  <TableCell colSpan={9} className="text-center text-muted-foreground">
                    Nenhuma stack contratada.
                  </TableCell>
                </TableRow>
              )}
              {row.stacks.map((s) => (
                <TableRow key={s.id}>
                  <TableCell className="font-medium">{s.slug}</TableCell>
                  <TableCell>
                    <Badge variant="secondary">{s.plan}</Badge>
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {s.machineId ? (
                      <Link href={`/machines/${s.machineId}`} className="hover:underline">
                        {s.machineName ?? s.machineId.slice(0, 8)}
                      </Link>
                    ) : (
                      "sem máquina"
                    )}
                  </TableCell>
                  <TableCell className="text-sm">{s.billingStatusDb}</TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {s.usageClass}
                  </TableCell>
                  <TableCell className="text-sm">
                    {formatRelativeDay(s.lastActivityAt)}
                  </TableCell>
                  <TableCell className="text-right font-mono text-sm tabular-nums">
                    {formatTokens(s.tokens)}
                  </TableCell>
                  <TableCell className="text-right font-mono text-sm tabular-nums">
                    {s.requests.toLocaleString("pt-BR")}
                  </TableCell>
                  <TableCell className="text-right font-mono text-sm tabular-nums text-muted-foreground">
                    {formatUsd(s.gpuCostUsd)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Chaves</CardTitle>
          <CardDescription>
            {keys.filter((k) => k.status === "active" && k.purpose === "customer").length}{" "}
            chave(s) de cliente ativa(s)
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Prefixo</TableHead>
                <TableHead>Nome</TableHead>
                <TableHead>Tipo</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Último uso</TableHead>
                <TableHead>Criada</TableHead>
                <TableHead>Expira</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {/* Falha de leitura NÃO pode se passar por "não existe": um erro
                  exibido como lista vazia faria o suporte concluir que o
                  cliente está sem chave e emitir outra. */}
              {keys.length === 0 && (
                <TableRow>
                  <TableCell
                    colSpan={7}
                    className={
                      keysError
                        ? "text-center text-destructive"
                        : "text-center text-muted-foreground"
                    }
                  >
                    {keysError
                      ? "Falha ao carregar as chaves — o valor não é zero, é desconhecido."
                      : "Nenhuma chave emitida."}
                  </TableCell>
                </TableRow>
              )}
              {keys.map((k) => (
                <TableRow key={k.id}>
                  <TableCell className="font-mono text-xs">{k.key_prefix}…</TableCell>
                  <TableCell className="text-sm">{k.name ?? "—"}</TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {k.purpose}
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant={k.status === "active" ? "success-light" : "outline"}
                    >
                      {k.status === "active" ? "Ativa" : "Revogada"}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-sm">
                    {formatRelativeDay(k.last_used_at)}
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {formatDate(k.created_at)}
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {k.expires_at ? formatDate(k.expires_at) : "nunca"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <p className="mt-4 text-xs text-muted-foreground">
            Somente leitura. A chave em texto puro não é exibida aqui — para
            emitir ou revogar, use a página de{" "}
            <Link href="/accounts" className="underline">
              Chaves
            </Link>
            .
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Ambientes</CardTitle>
          <CardDescription>
            Lugares de onde as stacks foram usadas nos últimos dias
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Ambiente</TableHead>
                <TableHead>Ferramenta</TableHead>
                <TableHead>Rede</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right!">Requests</TableHead>
                <TableHead>Primeiro uso</TableHead>
                <TableHead>Último uso</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {clients.length === 0 && (
                <TableRow>
                  <TableCell
                    colSpan={7}
                    className={
                      clientsError
                        ? "text-center text-destructive"
                        : "text-center text-muted-foreground"
                    }
                  >
                    {clientsError
                      ? "Falha ao carregar os ambientes — o valor não é zero, é desconhecido."
                      : "Nenhum ambiente registrado."}
                  </TableCell>
                </TableRow>
              )}
              {clients.map((c, i) => (
                <TableRow key={`${c.stack_id}-${i}`}>
                  <TableCell className="text-sm">{c.client_label ?? "—"}</TableCell>
                  <TableCell className="max-w-64 truncate text-xs text-muted-foreground">
                    {c.user_agent ?? "—"}
                  </TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">
                    {c.ip_bucket ?? "—"}
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant={c.status === "active" ? "success-light" : "outline"}
                    >
                      {c.status === "active" ? "Ativo" : "Liberado"}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right font-mono text-sm tabular-nums">
                    {c.requests.toLocaleString("pt-BR")}
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {formatDate(c.first_seen_at)}
                  </TableCell>
                  <TableCell className="text-sm">
                    {formatRelativeDay(c.last_seen_at)}
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

function Field({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="font-medium">{children}</span>
    </div>
  )
}

function InvoicesSkeleton() {
  return (
    <Card>
      <CardHeader className="flex flex-col gap-2">
        <Skeleton className="h-5 w-32" />
        <Skeleton className="h-3 w-48" />
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {Array.from({ length: 3 }).map((_, i) => (
          <Skeleton key={i} className="h-10 w-full" />
        ))}
      </CardContent>
    </Card>
  )
}
