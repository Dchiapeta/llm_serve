import Link from "next/link"
import {
  Activity,
  AlertTriangle,
  Coins,
  Download,
  Gauge,
  PiggyBank,
  Receipt,
  TrendingUp,
  Users,
} from "lucide-react"

import { formatUsd } from "@/lib/billing"
import { formatMoney } from "@/lib/chargefy-format"
import { formatTokens, parseScope, selectCrm, type CrmFilters } from "@/lib/crm"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { PeriodSwitch } from "@/components/dashboard/period-switch"
import { CrmTable } from "@/components/crm/crm-table"
import { CrmToolbar } from "@/components/crm/crm-toolbar"
import { MrrByPlanChart } from "@/components/crm/mrr-by-plan-chart"

import type { CrmSearchParams } from "./page"
import {
  CRM_PERIOD_LABELS,
  USD_BRL_RATE,
  getCrmData,
  parsePeriod,
} from "./queries"

const PERIOD_OPTIONS = [
  { value: "24h", label: "24 horas" },
  { value: "7d", label: "7 dias" },
  { value: "30d", label: "30 dias" },
  { value: "total", label: "Total" },
] as const

export async function CrmBody({
  searchParamsPromise,
}: {
  searchParamsPromise: Promise<CrmSearchParams>
}) {
  const sp = await searchParamsPromise
  const period = parsePeriod(sp.period)
  const escopo = parseScope(sp.escopo)

  const filters: CrmFilters = {
    q: sp.q ?? "",
    plano: sp.plano ?? "",
    cobranca: sp.cobranca ?? "",
    escopo,
  }

  const {
    rows,
    chargefy,
    unmatchedSubscriptions,
    unattributedTokens,
    usageTruncated,
    costTruncated,
    coreIncomplete,
    gpuCostTotalUsd,
  } = await getCrmData(period)

  // Filtra no servidor e deriva os KPIs do MESMO conjunto que a tabela mostra —
  // senão os cards somam um grupo e a listagem exibe outro.
  const { visible, kpis } = selectCrm(rows, filters)

  const financials = chargefy.ok
  const unmatchedCents = unmatchedSubscriptions.reduce(
    (s, u) => s + u.monthlyCents,
    0
  )

  // MRR por plano: a assinatura é da conta, então cada conta soma no plano de
  // maior degrau que ela contratou (é o que define o preço).
  const mrrByPlan = new Map<string, number>()
  for (const row of visible) {
    if (row.monthlyNetCents === 0) continue
    const plan = row.plans[row.plans.length - 1] ?? "Sem plano"
    mrrByPlan.set(plan, (mrrByPlan.get(plan) ?? 0) + row.monthlyNetCents)
  }
  const chartData = [...mrrByPlan.entries()].map(([label, mrr]) => ({
    label,
    mrr,
  }))

  const exportQs = new URLSearchParams()
  for (const [k, v] of Object.entries({ period, ...filters })) {
    if (v) exportQs.set(k === "period" ? "period" : k, String(v))
  }

  const kpiCards = [
    {
      label: "MRR",
      value: financials ? formatMoney(kpis.mrrCents) : "—",
      sub: financials
        ? `tabela ${formatMoney(kpis.mrrGrossCents)} · ${kpis.payingCustomers} pagante(s)`
        : "Chargefy indisponível",
      icon: TrendingUp,
    },
    {
      label: "Clientes",
      value: String(kpis.totalCustomers),
      sub: `${kpis.activeCustomers} com uso nos últimos 7 dias`,
      icon: Users,
    },
    {
      label: "Ticket médio",
      value: financials ? formatMoney(kpis.avgTicketCents) : "—",
      sub: "por cliente pagante",
      icon: Receipt,
    },
    {
      label: "Em risco",
      value: String(kpis.atRiskCustomers),
      sub: financials
        ? `${formatMoney(kpis.atRiskCents)} em atraso ou suspenso`
        : "em atraso, suspenso ou cancelado",
      icon: AlertTriangle,
    },
    {
      label: "Tokens no período",
      value: formatTokens(kpis.tokens),
      sub: `${kpis.requests.toLocaleString("pt-BR")} requisições`,
      icon: Activity,
    },
    {
      label: "Custo de GPU",
      value: formatUsd(kpis.gpuCostUsd),
      // O total é o número do Financeiro. Sem ele à vista, a diferença (máquina
      // sem stack, que custa e não tem dono) parece erro de conta.
      sub: `rateado · ${formatUsd(gpuCostTotalUsd)} de infra no total`,
      icon: Coins,
    },
    {
      label: "Margem estimada",
      value: financials && kpis.marginKnown ? formatMoney(kpis.marginBrlCents) : "—",
      sub: `receita − GPU rateada · câmbio R$ ${USD_BRL_RATE.toFixed(2)}/US$`,
      icon: PiggyBank,
    },
    {
      label: "Cobertura",
      value: `${kpis.withSubscription}/${kpis.totalCustomers}`,
      sub: "clientes com assinatura conhecida",
      icon: Gauge,
    },
  ]

  return (
    <>
      {!financials && (
        <Card className="border-destructive/40">
          <CardHeader className="flex flex-row items-start gap-3">
            <AlertTriangle className="mt-0.5 size-4 text-muted-foreground" />
            <div>
              <CardTitle className="text-base">
                Dados financeiros indisponíveis
              </CardTitle>
              <CardDescription>
                {chargefy.reason === "missing_key"
                  ? "CHARGEFY_SECRET_KEY não está configurada — as colunas de receita ficam vazias. O resto da página usa apenas o banco."
                  : `Falha ao consultar a Chargefy: ${chargefy.error}`}
              </CardDescription>
            </div>
          </CardHeader>
        </Card>
      )}

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {kpiCards.map((kpi) => (
          <Card key={kpi.label}>
            <CardHeader className="flex flex-row items-center justify-between pb-2">
              <CardDescription>{kpi.label}</CardDescription>
              <kpi.icon className="size-4 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              <p className="text-3xl font-semibold tracking-tight">{kpi.value}</p>
              <p className="mt-1 text-xs text-muted-foreground">{kpi.sub}</p>
            </CardContent>
          </Card>
        ))}
      </div>

      {financials && chartData.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Receita por plano</CardTitle>
            <CardDescription>
              MRR de cada degrau da escada comercial
            </CardDescription>
          </CardHeader>
          <CardContent>
            <MrrByPlanChart data={chartData} />
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <CardTitle>Clientes</CardTitle>
            <CardDescription>
              {visible.length} de {rows.length} conta(s) · uso de{" "}
              {CRM_PERIOD_LABELS[period]}
            </CardDescription>
          </div>
          <CrmToolbar
            q={filters.q ?? ""}
            plano={filters.plano ?? ""}
            cobranca={filters.cobranca ?? ""}
            actions={
              <>
                <PeriodSwitch period={period} options={PERIOD_OPTIONS} />
                <Button variant="outline" size="sm" asChild>
                  <a href={`/api/crm/export?${exportQs.toString()}`}>
                    <Download className="size-3.5" />
                    CSV
                  </a>
                </Button>
              </>
            }
          />
        </CardHeader>
        <CardContent>
          <CrmTable rows={visible} financialsAvailable={financials} />
        </CardContent>
      </Card>

      {(unmatchedSubscriptions.length > 0 ||
        unattributedTokens > 0 ||
        usageTruncated ||
        costTruncated ||
        coreIncomplete ||
        chargefy.truncated) && (
        <Card>
          <CardHeader>
            <CardTitle>Pendências de conciliação</CardTitle>
            <CardDescription>
              O que não encaixou — some daqui, não da conta
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-2 text-sm">
            {unmatchedSubscriptions.length > 0 && (
              <p>
                <strong>{unmatchedSubscriptions.length}</strong> assinatura(s) na
                Chargefy sem conta correspondente, somando{" "}
                {formatMoney(unmatchedCents)}/mês. Estão fora dos KPIs por conta,
                mas o dinheiro é real:{" "}
                <span className="font-mono text-xs text-muted-foreground">
                  {unmatchedSubscriptions.map((u) => u.id).join(", ")}
                </span>
              </p>
            )}
            {unattributedTokens > 0 && (
              <p>
                <strong>{formatTokens(unattributedTokens)}</strong> tokens em
                registros de uso sem stack nem chave identificável — não
                atribuídos a nenhum cliente.
              </p>
            )}
            {coreIncomplete && (
              <p className="text-destructive">
                A leitura de uma das tabelas base (contas, stacks, chaves,
                máquinas ou Chargefy) falhou ou excedeu o teto de páginas: pode
                haver cliente faltando nesta lista.
              </p>
            )}
            {usageTruncated && (
              <p className="text-destructive">
                A leitura de <code>usage_metrics</code> falhou ou excedeu o teto
                de páginas: os números de token estão incompletos.
              </p>
            )}
            {chargefy.truncated && (
              <p className="text-destructive">
                A Chargefy devolveu mais páginas que o teto da consulta: há
                assinaturas fora da conta e o MRR está incompleto.
              </p>
            )}
            {costTruncated && (
              <p className="text-destructive">
                A leitura de <code>machine_runtime_intervals</code> falhou ou
                excedeu o teto de páginas: o custo de GPU e a margem estão
                subestimados.
              </p>
            )}
          </CardContent>
        </Card>
      )}

      <p className="text-xs text-muted-foreground">
        Tokens e requisições vêm de <code>usage_metrics</code>, a mesma fonte que
        o gateway usa para aplicar a cota diária. A aba de Requisições usa{" "}
        <code>gateway_requests</code> (uma linha por request) e os totais não
        batem por construção. Cobrança vem da API da Chargefy ao vivo — o espelho
        no banco (<code>chargefy_subscriptions</code>) está vazio, então{" "}
        <Link href="/stacks" className="underline">
          o status de cobrança das stacks
        </Link>{" "}
        pode divergir.
      </p>
    </>
  )
}
