"use client"

import * as React from "react"
import Link from "next/link"
import { ChevronDown, ChevronRight, Copy } from "lucide-react"
import { toast } from "sonner"

import type { Account, Machine, RoutingState, Stack } from "@/lib/types"
import {
  Autocomplete,
  AutocompleteContent,
  AutocompleteEmpty,
  AutocompleteInput,
  AutocompleteItem,
  AutocompleteList,
} from "@/components/reui/autocomplete"
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
import { ContaRowActions } from "@/components/contas/conta-row-actions"

// Cor do badge por plano de produto — mantém a mesma paleta usada em
// components/templates para o plano do template.
const PLAN_BADGE_VARIANT: Record<Account["plan"], "secondary" | "info-light" | "success-light" | "warning-light"> = {
  VibeCoder: "secondary",
  Pro: "info-light",
  Max: "success-light",
  Enterprise: "warning-light",
}

export type ContaRow = {
  account: Account
  route: RoutingState | undefined
  currentMachine: Pick<Machine, "id" | "name"> | undefined
  hasReadyAdapter: boolean
  knowledgeFiles: { storage_path: string; chunks: number }[]
  tokens: number
  stacks: Stack[]
}

// purchase_date é date puro ("YYYY-MM-DD"); anexar meia-noite local evita
// o off-by-one de fuso ao formatar.
function formatPurchaseDate(date: string) {
  return new Date(`${date}T00:00:00`).toLocaleDateString("pt-BR")
}

export function ContasTable({
  rows,
  runningMachines,
  periodLabel,
}: {
  rows: ContaRow[]
  runningMachines: Machine[]
  periodLabel: string
}) {
  const [query, setQuery] = React.useState("")
  const [expanded, setExpanded] = React.useState<Set<string>>(new Set())

  function toggleExpanded(accountId: string) {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(accountId)) next.delete(accountId)
      else next.add(accountId)
      return next
    })
  }

  function copySlug(slug: string) {
    navigator.clipboard.writeText(slug)
    toast.success("ID copiado")
  }

  const emailItems = React.useMemo(
    () =>
      rows
        .filter((r) => r.account.email)
        .map((r) => ({
          value: r.account.email as string,
          label: `${r.account.name} — ${r.account.email}`,
        })),
    [rows]
  )

  const normalizedQuery = query.trim().toLowerCase()
  const filteredRows = normalizedQuery
    ? rows.filter((r) => r.account.email?.toLowerCase().includes(normalizedQuery))
    : rows

  return (
    <div className="flex flex-col gap-4">
      <Autocomplete
        items={emailItems}
        value={query}
        onValueChange={setQuery}
        // Ao clicar num item, preenche o input com o e-mail (sem isso,
        // itens {value,label} usariam o label "Nome — e-mail").
        itemToStringValue={(item) => item.value}
        filter={(item, q) =>
          item.label.toLowerCase().includes(q.trim().toLowerCase())
        }
      >
        <AutocompleteInput
          placeholder="Buscar por e-mail…"
          showClear
          className="max-w-xs"
        />
        <AutocompleteContent>
          <AutocompleteEmpty>Nenhuma conta com esse e-mail.</AutocompleteEmpty>
          <AutocompleteList>
            {(item) => (
              <AutocompleteItem key={item.value} value={item}>
                {item.label}
              </AutocompleteItem>
            )}
          </AutocompleteList>
        </AutocompleteContent>
      </Autocomplete>

      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="w-8" />
            <TableHead>Conta</TableHead>
            <TableHead>Plano</TableHead>
            <TableHead>Máquina atual</TableHead>
            <TableHead>Consumo de tokens ({periodLabel.toLowerCase()})</TableHead>
            <TableHead className="w-10" />
          </TableRow>
        </TableHeader>
        <TableBody>
          {filteredRows.length === 0 && (
            <TableRow>
              <TableCell colSpan={6} className="text-center text-muted-foreground">
                {rows.length === 0
                  ? "Nenhuma conta ainda."
                  : "Nenhuma conta encontrada para esse e-mail."}
              </TableCell>
            </TableRow>
          )}
          {filteredRows.map(
            ({ account, route, currentMachine, hasReadyAdapter, knowledgeFiles, tokens, stacks }) => (
            <React.Fragment key={account.id}>
            <TableRow>
              <TableCell className="pr-0">
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-6"
                  onClick={() => toggleExpanded(account.id)}
                  aria-label={
                    expanded.has(account.id) ? "Recolher stacks" : "Expandir stacks"
                  }
                >
                  {expanded.has(account.id) ? (
                    <ChevronDown className="size-4" />
                  ) : (
                    <ChevronRight className="size-4" />
                  )}
                </Button>
              </TableCell>
              <TableCell>
                <div className="flex items-center gap-2">
                  <p className="text-sm font-medium">{account.name}</p>
                  <Badge variant="secondary" size="sm">
                    {stacks.length} stack{stacks.length === 1 ? "" : "s"}
                  </Badge>
                </div>
                {account.email && (
                  <p className="text-xs text-muted-foreground">{account.email}</p>
                )}
              </TableCell>
              <TableCell>
                <Badge variant={PLAN_BADGE_VARIANT[account.plan]} size="sm">
                  {account.plan}
                </Badge>
              </TableCell>
              <TableCell>
                {currentMachine ? (
                  <Link
                    href={`/machines/${currentMachine.id}`}
                    className="text-sm hover:underline"
                  >
                    {currentMachine.name}
                  </Link>
                ) : (
                  <span className="text-sm text-muted-foreground">—</span>
                )}
              </TableCell>
              <TableCell className="text-sm tabular-nums">
                {tokens.toLocaleString("pt-BR")}
              </TableCell>
              <TableCell>
                <ContaRowActions
                  account={account}
                  route={route}
                  currentMachineName={currentMachine?.name}
                  eligibleMachines={runningMachines.filter(
                    (m) => m.id !== route?.machine_id
                  )}
                  hasReadyAdapter={hasReadyAdapter}
                  knowledgeFiles={knowledgeFiles}
                />
              </TableCell>
            </TableRow>
            {expanded.has(account.id) && (
              <TableRow className="hover:bg-transparent">
                <TableCell colSpan={6} className="bg-muted/30 p-0">
                  {stacks.length === 0 ? (
                    <p className="px-10 py-3 text-sm text-muted-foreground">
                      Nenhuma stack.
                    </p>
                  ) : (
                    <Table>
                      <TableHeader>
                        <TableRow className="hover:bg-transparent">
                          <TableHead className="pl-10">Produto</TableHead>
                          <TableHead>ID (subdomínio)</TableHead>
                          <TableHead>Data da compra</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {stacks.map((stack) => (
                          <TableRow key={stack.id} className="hover:bg-transparent">
                            <TableCell className="pl-10">
                              <Badge variant={PLAN_BADGE_VARIANT[stack.plan]} size="sm">
                                {stack.plan}
                              </Badge>
                            </TableCell>
                            <TableCell>
                              <div className="flex items-center gap-1">
                                <code className="font-mono text-xs">{stack.slug}</code>
                                <Button
                                  variant="ghost"
                                  size="icon"
                                  className="size-6"
                                  onClick={() => copySlug(stack.slug)}
                                  aria-label="Copiar ID"
                                >
                                  <Copy className="size-3" />
                                </Button>
                              </div>
                            </TableCell>
                            <TableCell className="text-sm">
                              {formatPurchaseDate(stack.purchase_date)}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  )}
                </TableCell>
              </TableRow>
            )}
            </React.Fragment>
            )
          )}
        </TableBody>
      </Table>
    </div>
  )
}
