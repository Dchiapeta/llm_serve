"use client"

import * as React from "react"
import { ArrowDown, ArrowUp, ChevronsUpDown } from "lucide-react"

import type { BillingStatus, Machine, TemplatePlan } from "@/lib/types"
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
import { CopyableId } from "@/components/contas/copyable-id"

// Stack vista da conta: o suficiente pro dialog de info e pra guarda do
// delete. machineStatus null = stack sem máquina (nunca alocada ou órfã de
// uma máquina já terminada).
export type ContaStackSummary = {
  id: string
  name: string
  slug: string
  plan: TemplatePlan
  billingStatus: BillingStatus
  machineName: string | null
  machineStatus: Machine["status"] | null
}

export type UsuarioRow = {
  id: string
  name: string
  email: string | null
  userId: string | null
  stacks: number
  stackList: ContaStackSummary[]
  tokens: number
  requests: number
  createdAt: string
}

type SortKey = "name" | "email" | "stacks" | "tokens" | "requests" | "createdAt"
type SortDir = "asc" | "desc"

// Colunas numéricas/data começam maior→menor; texto começa A→Z.
const NUMERIC: Record<SortKey, boolean> = {
  name: false,
  email: false,
  stacks: true,
  tokens: true,
  requests: true,
  createdAt: true,
}

export function UsuariosTable({ rows }: { rows: UsuarioRow[] }) {
  // Começa ordenado por uso de token (maior primeiro), o foco da página.
  const [sortKey, setSortKey] = React.useState<SortKey>("tokens")
  const [sortDir, setSortDir] = React.useState<SortDir>("desc")

  const sorted = React.useMemo(() => {
    const copy = [...rows]
    copy.sort((a, b) => {
      let cmp: number
      switch (sortKey) {
        case "name":
          cmp = a.name.localeCompare(b.name, "pt-BR")
          break
        case "email":
          cmp = (a.email ?? "").localeCompare(b.email ?? "", "pt-BR")
          break
        case "createdAt":
          cmp = a.createdAt.localeCompare(b.createdAt)
          break
        default:
          cmp = (a[sortKey] as number) - (b[sortKey] as number)
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
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>ID</TableHead>
          <SortableHead label="Nome" col="name" {...headProps} />
          <SortableHead label="E-mail" col="email" {...headProps} />
          <SortableHead label="Stacks" col="stacks" {...headProps} />
          <SortableHead label="Tokens" col="tokens" {...headProps} />
          <SortableHead label="Requests" col="requests" {...headProps} />
          <SortableHead label="Criada em" col="createdAt" {...headProps} />
          <TableHead className="w-10" />
        </TableRow>
      </TableHeader>
      <TableBody>
        {sorted.length === 0 && (
          <TableRow>
            <TableCell colSpan={8} className="text-center text-muted-foreground">
              Nenhuma conta ainda.
            </TableCell>
          </TableRow>
        )}
        {sorted.map((u) => (
          <TableRow key={u.id}>
            <TableCell>
              <CopyableId value={u.id} />
            </TableCell>
            <TableCell className="text-sm font-medium">{u.name}</TableCell>
            <TableCell className="text-sm text-muted-foreground">
              {u.email ?? "—"}
            </TableCell>
            <TableCell className="text-sm tabular-nums">{u.stacks}</TableCell>
            <TableCell className="text-sm tabular-nums">
              {u.tokens.toLocaleString("pt-BR")}
            </TableCell>
            <TableCell className="text-sm tabular-nums">
              {u.requests.toLocaleString("pt-BR")}
            </TableCell>
            <TableCell
              className="text-sm whitespace-nowrap"
              title={new Date(u.createdAt).toLocaleString("pt-BR")}
            >
              {new Date(u.createdAt).toLocaleDateString("pt-BR")}
            </TableCell>
            <TableCell>
              <ContaRowActions conta={u} />
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function SortableHead({
  label,
  col,
  sortKey,
  sortDir,
  onSort,
}: {
  label: string
  col: SortKey
  sortKey: SortKey
  sortDir: SortDir
  onSort: (key: SortKey) => void
}) {
  const active = sortKey === col
  const Icon = !active ? ChevronsUpDown : sortDir === "asc" ? ArrowUp : ArrowDown
  return (
    <TableHead>
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
