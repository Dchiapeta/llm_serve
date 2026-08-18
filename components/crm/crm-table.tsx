"use client"

import * as React from "react"
import Link from "next/link"
import {
  ArrowDown,
  ArrowUp,
  ChevronsUpDown,
  CircleAlert,
  TriangleAlert,
} from "lucide-react"

import { formatRelativeDay, formatTokens, type CrmRow } from "@/lib/crm"
import { BILLING_BADGE, graceRemaining } from "@/lib/billing-status"
import { formatMoney } from "@/lib/chargefy-format"
import { formatUsd } from "@/lib/billing"
import { Badge } from "@/components/reui/badge"
import { Button } from "@/components/ui/button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"

type SortKey =
  | "name"
  | "monthly"
  | "lastUsed"
  | "tokens"
  | "requests"
  | "keys"
  | "envs"
  | "gpu"
  | "margin"
  | "since"
type SortDir = "asc" | "desc"

// Colunas numéricas/data começam maior→menor; texto começa A→Z.
const NUMERIC: Record<SortKey, boolean> = {
  name: false,
  monthly: true,
  lastUsed: true,
  tokens: true,
  requests: true,
  keys: true,
  envs: true,
  gpu: true,
  margin: true,
  since: true,
}

// Recebe as linhas JÁ filtradas pelo servidor; aqui só se ordena. Filtrar
// também aqui faria a contagem do cabeçalho divergir do que a tabela mostra.
export function CrmTable({
  rows,
  financialsAvailable,
}: {
  rows: CrmRow[]
  financialsAvailable: boolean
}) {
  // Começa por receita: o CRM é uma visão comercial antes de ser técnica.
  const [sortKey, setSortKey] = React.useState<SortKey>("monthly")
  const [sortDir, setSortDir] = React.useState<SortDir>("desc")

  const sorted = React.useMemo(() => {
    const copy = [...rows]
    copy.sort((a, b) => {
      let cmp: number
      switch (sortKey) {
        case "name":
          cmp = a.name.localeCompare(b.name, "pt-BR")
          break
        case "lastUsed":
          cmp = (a.lastUsedAt ?? "").localeCompare(b.lastUsedAt ?? "")
          break
        case "since":
          cmp = a.customerSince.localeCompare(b.customerSince)
          break
        case "monthly":
          cmp = a.monthlyNetCents - b.monthlyNetCents
          break
        case "tokens":
          cmp = a.tokens - b.tokens
          break
        case "requests":
          cmp = a.requests - b.requests
          break
        case "keys":
          cmp = a.activeKeys - b.activeKeys
          break
        case "envs":
          cmp = a.envs - b.envs
          break
        case "gpu":
          cmp = a.gpuCostUsd - b.gpuCostUsd
          break
        default:
          cmp = (a.marginBrlCents ?? 0) - (b.marginBrlCents ?? 0)
      }
      return sortDir === "asc" ? cmp : -cmp
    })
    return copy
  }, [rows, sortKey, sortDir])

  function toggleSort(key: SortKey) {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"))
    } else {
      setSortKey(key)
      setSortDir(NUMERIC[key] ? "desc" : "asc")
    }
  }

  const headProps = { sortKey, sortDir, onSort: toggleSort }

  return (
    <div className="overflow-x-auto">
      <Table>
        <TableHeader>
          <TableRow>
            <SortableHead label="Cliente" col="name" {...headProps} />
            <TableHead>Planos</TableHead>
            <TableHead>Cobrança</TableHead>
            <SortableHead label="Mensal" col="monthly" align="right" {...headProps} />
            <SortableHead label="Último uso" col="lastUsed" {...headProps} />
            <SortableHead label="Tokens" col="tokens" align="right" {...headProps} />
            <SortableHead label="Requests" col="requests" align="right" {...headProps} />
            <SortableHead label="Chaves" col="keys" align="right" {...headProps} />
            <SortableHead label="Ambientes" col="envs" align="right" {...headProps} />
            <SortableHead label="Custo GPU" col="gpu" align="right" {...headProps} />
            <SortableHead label="Margem" col="margin" align="right" {...headProps} />
          </TableRow>
        </TableHeader>
        <TableBody>
          {sorted.length === 0 && (
            <TableRow>
              <TableCell
                colSpan={11}
                className="text-center text-muted-foreground"
              >
                Nenhum cliente encontrado com esses filtros.
              </TableCell>
            </TableRow>
          )}
          {sorted.map((row) => {
            const badge = row.billingStatus
              ? BILLING_BADGE[row.billingStatus]
              : null
            const grace = graceRemaining(row.billingStatus, row.pastDueSince)
            return (
              <TableRow key={row.accountId}>
                <TableCell>
                  <div className="flex flex-col">
                    <Link
                      href={`/crm/${row.accountId}`}
                      className="text-sm font-medium hover:underline"
                    >
                      {row.name}
                    </Link>
                    <span className="text-xs text-muted-foreground">
                      {row.email ?? "sem e-mail"}
                    </span>
                  </div>
                </TableCell>

                <TableCell>
                  <div className="flex flex-wrap items-center gap-1">
                    {row.plans.length === 0 && (
                      <span className="text-sm text-muted-foreground">—</span>
                    )}
                    {row.plans.map((plan) => (
                      <Badge key={plan} variant="secondary">
                        {plan}
                      </Badge>
                    ))}
                    {row.stacks.length > 1 && (
                      <span className="text-xs text-muted-foreground">
                        {row.stacks.length} stacks
                      </span>
                    )}
                  </div>
                </TableCell>

                <TableCell>
                  <div className="flex items-center gap-1.5">
                    {badge ? (
                      <Badge variant={badge.variant}>{badge.label}</Badge>
                    ) : row.billingStatus ? (
                      <span className="text-sm text-muted-foreground">Em dia</span>
                    ) : (
                      <span className="text-sm text-muted-foreground">—</span>
                    )}
                    {grace && (
                      <span className="text-xs text-muted-foreground">{grace}</span>
                    )}
                    {row.subscriptionDivergence && (
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <CircleAlert className="size-3.5 text-muted-foreground" />
                        </TooltipTrigger>
                        <TooltipContent>
                          Banco diz &ldquo;{row.billingStatus}&rdquo;, Chargefy diz
                          &ldquo;{row.subscriptionStatuses.join(", ")}&rdquo;
                        </TooltipContent>
                      </Tooltip>
                    )}
                    {row.orphanSubscriptions > 0 && (
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <TriangleAlert className="size-3.5 text-muted-foreground" />
                        </TooltipTrigger>
                        <TooltipContent>
                          {row.orphanSubscriptions} assinatura(s) paga(s) sem stack
                          provisionada
                        </TooltipContent>
                      </Tooltip>
                    )}
                  </div>
                </TableCell>

                <TableCell className="text-right">
                  {!financialsAvailable ? (
                    <span className="text-sm text-muted-foreground">—</span>
                  ) : row.monthlyNetCents === 0 ? (
                    <span className="text-sm text-muted-foreground">—</span>
                  ) : (
                    <div className="flex flex-col items-end">
                      <span className="font-mono text-sm tabular-nums">
                        {formatMoney(row.monthlyNetCents, row.currency)}
                      </span>
                      {row.hasDiscount && (
                        <span className="text-xs text-muted-foreground line-through">
                          {formatMoney(row.monthlyGrossCents, row.currency)}
                        </span>
                      )}
                      {row.mixedCurrency && (
                        <span className="text-xs text-destructive">
                          moedas misturadas
                        </span>
                      )}
                    </div>
                  )}
                </TableCell>

                <TableCell className="text-sm">
                  {formatRelativeDay(row.lastUsedAt)}
                </TableCell>

                <TableCell className="text-right font-mono text-sm tabular-nums">
                  {formatTokens(row.tokens)}
                </TableCell>
                <TableCell className="text-right font-mono text-sm tabular-nums">
                  {row.requests.toLocaleString("pt-BR")}
                </TableCell>
                <TableCell className="text-right font-mono text-sm tabular-nums">
                  {row.activeKeys}
                  {row.keyLimit !== null && (
                    <span className="text-muted-foreground">/{row.keyLimit}</span>
                  )}
                </TableCell>
                <TableCell className="text-right font-mono text-sm tabular-nums">
                  {row.envs}
                  {row.envLimit !== null && (
                    <span className="text-muted-foreground">/{row.envLimit}</span>
                  )}
                </TableCell>
                <TableCell className="text-right font-mono text-sm tabular-nums text-muted-foreground">
                  {formatUsd(row.gpuCostUsd)}
                </TableCell>
                <TableCell className="text-right font-mono text-sm tabular-nums">
                  {row.marginBrlCents === null || !financialsAvailable ? (
                    <span className="text-muted-foreground">—</span>
                  ) : (
                    formatMoney(row.marginBrlCents, "BRL")
                  )}
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    </div>
  )
}

function SortableHead({
  label,
  col,
  sortKey,
  sortDir,
  onSort,
  align = "left",
}: {
  label: string
  col: SortKey
  sortKey: SortKey
  sortDir: SortDir
  onSort: (key: SortKey) => void
  align?: "left" | "right"
}) {
  const active = sortKey === col
  const Icon = !active ? ChevronsUpDown : sortDir === "asc" ? ArrowUp : ArrowDown
  return (
    <TableHead className={align === "right" ? "text-right!" : undefined}>
      <Button
        variant="ghost"
        size="sm"
        onClick={() => onSort(col)}
        className="-ml-2 h-8 gap-1 px-2 data-[active=true]:text-foreground"
        data-active={active}
      >
        {label}
        <Icon
          className={active ? "size-3.5" : "size-3.5 text-muted-foreground/60"}
        />
      </Button>
    </TableHead>
  )
}
