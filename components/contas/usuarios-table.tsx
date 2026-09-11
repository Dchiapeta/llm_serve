"use client"

import * as React from "react"
import { ArrowDown, ArrowUp, ChevronsUpDown, Search } from "lucide-react"

import {
  TEMPLATE_PLANS,
  type BillingStatus,
  type Machine,
  type ProductCategory,
  type TemplatePlan,
} from "@/lib/types"
import { Button } from "@/components/ui/button"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupInput,
} from "@/components/ui/input-group"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
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
  category: ProductCategory
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

const ALL = "__all__"

// TEMPLATE_PLANS é só a escada comercial; "Image" entra no fim porque é outra
// linha de produto, não um degrau dela (mesma razão do PLAN_BADGE_VARIANT).
const PLAN_ORDER: TemplatePlan[] = [...TEMPLATE_PLANS, "Image"]

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
  const [query, setQuery] = React.useState("")
  const [planFilter, setPlanFilter] = React.useState<string>(ALL)

  // Só os planos que existem na base, na ordem da escada: uma opção que não
  // filtra nada só dá trabalho pro suporte.
  const planOptions = React.useMemo(() => {
    const present = new Set(rows.flatMap((u) => u.stackList.map((s) => s.plan)))
    return PLAN_ORDER.filter((plan) => present.has(plan))
  }, [rows])

  const filtered = React.useMemo(() => {
    const q = query.trim().toLowerCase()
    return rows.filter((u) => {
      const matchesQuery =
        !q ||
        u.name.toLowerCase().includes(q) ||
        (u.email?.toLowerCase().includes(q) ?? false)
      // Conta com várias stacks casa se qualquer uma delas for do plano.
      const matchesPlan =
        planFilter === ALL || u.stackList.some((s) => s.plan === planFilter)
      return matchesQuery && matchesPlan
    })
  }, [rows, query, planFilter])

  const sorted = React.useMemo(() => {
    const copy = [...filtered]
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
  }, [filtered, sortKey, sortDir])

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
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <InputGroup className="max-w-xs">
          <InputGroupAddon>
            <Search className="size-4 text-muted-foreground" />
          </InputGroupAddon>
          <InputGroupInput
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Buscar por nome ou e-mail…"
          />
        </InputGroup>

        <Select value={planFilter} onValueChange={setPlanFilter}>
          <SelectTrigger className="w-40">
            <SelectValue placeholder="Plano" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>Todos os planos</SelectItem>
            {planOptions.map((plan) => (
              <SelectItem key={plan} value={plan}>
                {plan}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

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
              <TableCell
                colSpan={8}
                className="text-center text-muted-foreground"
              >
                {query.trim() || planFilter !== ALL
                  ? "Nenhuma conta encontrada."
                  : "Nenhuma conta ainda."}
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
    </div>
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
