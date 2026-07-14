"use client"

import * as React from "react"
import Link from "next/link"
import { Copy, Search } from "lucide-react"
import { toast } from "sonner"

import type { Account, ApiKey, Machine, RoutingState, Stack } from "@/lib/types"
import { Badge } from "@/components/reui/badge"
import { Button } from "@/components/ui/button"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupInput,
} from "@/components/ui/input-group"
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
export const PLAN_BADGE_VARIANT: Record<Account["plan"], "secondary" | "info-light" | "success-light" | "warning-light"> = {
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

  function copySlug(slug: string) {
    navigator.clipboard.writeText(slug)
    toast.success("ID copiado")
  }

  const normalizedQuery = query.trim().toLowerCase()
  const filteredRows = normalizedQuery
    ? rows.filter(
        (r) =>
          r.stack.slug.toLowerCase().includes(normalizedQuery) ||
          r.account.name.toLowerCase().includes(normalizedQuery) ||
          r.account.email?.toLowerCase().includes(normalizedQuery)
      )
    : rows

  return (
    <div className="flex flex-col gap-4">
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

      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Stack</TableHead>
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
              <TableCell colSpan={7} className="text-center text-muted-foreground">
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
