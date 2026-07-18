"use client"

import * as React from "react"
import Link from "next/link"
import { Copy, Search } from "lucide-react"
import { toast } from "sonner"

import { TEMPLATE_PLANS, type Account, type ApiKey, type Machine, type RoutingState, type Stack, type TemplatePlan } from "@/lib/types"
import { Badge } from "@/components/reui/badge"
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
import type {
  StackMachine,
  StackTemplate,
} from "@/components/contas/create-stack-dialog"
import { StackRowActions } from "@/components/contas/stack-row-actions"

// Cor do badge por plano de produto — mantém a mesma paleta usada em
// components/templates para o plano do template.
export const PLAN_BADGE_VARIANT: Record<TemplatePlan, "secondary" | "info-light" | "success-light" | "warning-light"> = {
  VibeCoder: "secondary",
  Pro: "info-light",
  Max: "success-light",
  Enterprise: "warning-light",
}

// Status da máquina visto da stack; sem máquina (machine_id nulo ou
// máquina terminada) a stack aparece como "Desativada".
const MACHINE_STATUS_BADGE: Record<
  Machine["status"],
  { label: string; variant: React.ComponentProps<typeof Badge>["variant"] }
> = {
  running: { label: "Rodando", variant: "success-light" },
  stopped: { label: "Pausado", variant: "secondary" },
  creating: { label: "Criando", variant: "warning-light" },
  error: { label: "Erro", variant: "destructive-light" },
  terminated: { label: "Desativada", variant: "outline" },
}

const NO_MACHINE_BADGE = { label: "Desativada", variant: "outline" } as const

// Opções do filtro de status — rótulos exibidos na tabela; "terminated"
// fica de fora porque `machines` (page.tsx) já exclui máquinas encerradas.
const STATUS_FILTER_OPTIONS = [
  { value: "running", label: "Rodando" },
  { value: "stopped", label: "Pausado" },
  { value: "creating", label: "Criando" },
  { value: "error", label: "Erro" },
  { value: "none", label: "Desativada" },
] as const

const ALL = "__all__"

export type StackInfo = Stack & {
  machineName?: string
  machine?: Pick<
    Machine,
    | "id"
    | "name"
    | "gpu_type"
    | "status"
    | "model_name"
    | "vram_gb"
    | "cost_per_hr"
    | "public_url"
    | "max_users"
    | "template_id"
  >
  templateName?: string
  keys: {
    key_prefix: string
    plain_key: string | null
    status: ApiKey["status"]
    created_at: string
  }[]
  usage: { tokensIn: number; tokensOut: number; requests: number }
}

export type StackRow = {
  stack: StackInfo
  account: Account
  route: RoutingState | undefined
  currentMachine: Pick<Machine, "id" | "name"> | undefined
  hasReadyAdapter: boolean
  knowledgeFiles: { storage_path: string; chunks: number }[]
}

// purchase_date é date puro ("YYYY-MM-DD"); anexar meia-noite local evita
// o off-by-one de fuso ao formatar.
export function formatPurchaseDate(date: string) {
  return new Date(`${date}T00:00:00`).toLocaleDateString("pt-BR")
}

export function ContasTable({
  rows,
  runningMachines,
  periodLabel,
  stackMachines,
  templates,
}: {
  rows: StackRow[]
  runningMachines: Machine[]
  periodLabel: string
  stackMachines: StackMachine[]
  templates: StackTemplate[]
}) {
  const [query, setQuery] = React.useState("")
  const [productFilter, setProductFilter] = React.useState(ALL)
  const [statusFilter, setStatusFilter] = React.useState(ALL)

  function copySlug(slug: string) {
    navigator.clipboard.writeText(slug)
    toast.success("ID copiado")
  }

  function copyStackId(stackId: string) {
    navigator.clipboard.writeText(stackId)
    toast.success("Stack ID copiado")
  }

  const normalizedQuery = query.trim().toLowerCase()
  const filteredRows = rows.filter((r) => {
    const matchesQuery = normalizedQuery
      ? r.stack.slug.toLowerCase().includes(normalizedQuery) ||
        r.account.name.toLowerCase().includes(normalizedQuery) ||
        r.account.email?.toLowerCase().includes(normalizedQuery)
      : true
    const matchesProduct = productFilter === ALL || r.stack.plan === productFilter
    const statusValue = r.stack.machine?.status ?? "none"
    const matchesStatus = statusFilter === ALL || statusValue === statusFilter
    return matchesQuery && matchesProduct && matchesStatus
  })

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
            placeholder="Buscar por stack, cliente ou e-mail…"
          />
        </InputGroup>

        <Select value={productFilter} onValueChange={setProductFilter}>
          <SelectTrigger className="w-40">
            <SelectValue placeholder="Produto" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>Todos os produtos</SelectItem>
            {TEMPLATE_PLANS.map((plan) => (
              <SelectItem key={plan} value={plan}>
                {plan}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <Select value={statusFilter} onValueChange={setStatusFilter}>
          <SelectTrigger className="w-40">
            <SelectValue placeholder="Status" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>Todos os status</SelectItem>
            {STATUS_FILTER_OPTIONS.map((s) => (
              <SelectItem key={s.value} value={s.value}>
                {s.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Stack</TableHead>
            <TableHead>Stack ID</TableHead>
            <TableHead>Cliente</TableHead>
            <TableHead>E-mail</TableHead>
            <TableHead>Produto</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Máquina</TableHead>
            <TableHead className="w-10" />
          </TableRow>
        </TableHeader>
        <TableBody>
          {filteredRows.length === 0 && (
            <TableRow>
              <TableCell colSpan={8} className="text-center text-muted-foreground">
                {rows.length === 0
                  ? "Nenhuma stack ainda."
                  : "Nenhuma stack encontrada."}
              </TableCell>
            </TableRow>
          )}
          {filteredRows.map((row) => {
            const { stack, account } = row
            const status = stack.machine
              ? MACHINE_STATUS_BADGE[stack.machine.status]
              : NO_MACHINE_BADGE
            return (
              <TableRow key={stack.id}>
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
                <TableCell>
                  <div className="flex items-center gap-1">
                    <code
                      className="max-w-32 truncate font-mono text-xs"
                      title={stack.id}
                    >
                      {stack.id}
                    </code>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="size-6 shrink-0"
                      onClick={() => copyStackId(stack.id)}
                      aria-label="Copiar Stack ID"
                    >
                      <Copy className="size-3" />
                    </Button>
                  </div>
                </TableCell>
                <TableCell className="text-sm font-medium">
                  {account.name}
                </TableCell>
                <TableCell className="text-sm text-muted-foreground">
                  {account.email ?? "—"}
                </TableCell>
                <TableCell>
                  <Badge variant={PLAN_BADGE_VARIANT[stack.plan]} size="sm">
                    {stack.plan}
                  </Badge>
                </TableCell>
                <TableCell>
                  <Badge variant={status.variant} size="sm">
                    {status.label}
                  </Badge>
                </TableCell>
                <TableCell>
                  {stack.machine ? (
                    <Link
                      href={`/machines/${stack.machine.id}`}
                      className="text-sm hover:underline"
                    >
                      {stack.machine.name}
                    </Link>
                  ) : (
                    <span className="text-sm text-muted-foreground">—</span>
                  )}
                </TableCell>
                <TableCell>
                  <StackRowActions
                    row={row}
                    periodLabel={periodLabel}
                    runningMachines={runningMachines}
                    stackMachines={stackMachines}
                    templates={templates}
                  />
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    </div>
  )
}
