"use client"

import * as React from "react"
import Link from "next/link"

import type { Account, Machine, RoutingState } from "@/lib/types"
import {
  Autocomplete,
  AutocompleteContent,
  AutocompleteEmpty,
  AutocompleteInput,
  AutocompleteItem,
  AutocompleteList,
} from "@/components/reui/autocomplete"
import { Badge } from "@/components/reui/badge"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { ContaRowActions } from "@/components/contas/conta-row-actions"

export type ContaRow = {
  account: Account
  route: RoutingState | undefined
  currentMachine: Pick<Machine, "id" | "name"> | undefined
  plan: "Básico" | "Avançado"
  tokens: number
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
      <Autocomplete items={emailItems} value={query} onValueChange={setQuery}>
        <AutocompleteInput
          placeholder="Buscar por e-mail…"
          showClear
          className="max-w-xs"
        />
        <AutocompleteContent>
          <AutocompleteEmpty>Nenhuma conta com esse e-mail.</AutocompleteEmpty>
          <AutocompleteList>
            {(item) => (
              <AutocompleteItem key={item.value} value={item.value}>
                {item.label}
              </AutocompleteItem>
            )}
          </AutocompleteList>
        </AutocompleteContent>
      </Autocomplete>

      <Table>
        <TableHeader>
          <TableRow>
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
              <TableCell colSpan={5} className="text-center text-muted-foreground">
                {rows.length === 0
                  ? "Nenhuma conta ainda."
                  : "Nenhuma conta encontrada para esse e-mail."}
              </TableCell>
            </TableRow>
          )}
          {filteredRows.map(({ account, route, currentMachine, plan, tokens }) => (
            <TableRow key={account.id}>
              <TableCell>
                <p className="text-sm font-medium">{account.name}</p>
                {account.email && (
                  <p className="text-xs text-muted-foreground">{account.email}</p>
                )}
              </TableCell>
              <TableCell>
                <Badge variant={plan === "Avançado" ? "info-light" : "secondary"} size="sm">
                  {plan}
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
                  plan={plan}
                  eligibleMachines={runningMachines.filter(
                    (m) => m.id !== route?.machine_id
                  )}
                  hasReadyAdapter={plan === "Avançado"}
                />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}
