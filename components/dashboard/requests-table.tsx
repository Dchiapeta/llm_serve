"use client"

import * as React from "react"
import { Search } from "lucide-react"

import type { GatewayRequest } from "@/lib/types"
import { Badge } from "@/components/ui/badge"
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

const ALL = "__all__"

export type RequestRow = GatewayRequest & {
  stacks: { slug: string } | null
  api_keys: { key_prefix: string } | null
  accounts: { name: string } | null
}

export function RequestsTable({ rows }: { rows: RequestRow[] }) {
  const [query, setQuery] = React.useState("")
  const [statusFilter, setStatusFilter] = React.useState(ALL)

  const normalizedQuery = query.trim().toLowerCase()
  const filteredRows = rows.filter((r) => {
    const matchesQuery = normalizedQuery
      ? (r.stacks?.slug.toLowerCase().includes(normalizedQuery) ?? false) ||
        (r.accounts?.name.toLowerCase().includes(normalizedQuery) ?? false) ||
        (r.api_keys?.key_prefix.toLowerCase().includes(normalizedQuery) ?? false)
      : true
    const matchesStatus =
      statusFilter === ALL ||
      (statusFilter === "ok" ? r.status_code < 400 : r.status_code >= 400)
    return matchesQuery && matchesStatus
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
            placeholder="Buscar por stack, conta ou chave…"
          />
        </InputGroup>

        <Select value={statusFilter} onValueChange={setStatusFilter}>
          <SelectTrigger className="w-36">
            <SelectValue placeholder="Status" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>Todos os status</SelectItem>
            <SelectItem value="ok">Sucesso</SelectItem>
            <SelectItem value="error">Erro</SelectItem>
          </SelectContent>
        </Select>
      </div>

      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Hora</TableHead>
            <TableHead>Stack</TableHead>
            <TableHead>Conta</TableHead>
            <TableHead>Chave</TableHead>
            <TableHead>Endpoint</TableHead>
            <TableHead>Modelo</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Stream</TableHead>
            <TableHead className="text-right">Tokens in/out</TableHead>
            <TableHead className="text-right">Duração</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {filteredRows.length === 0 && (
            <TableRow>
              <TableCell colSpan={10} className="text-center text-muted-foreground">
                Nenhuma requisição encontrada.
              </TableCell>
            </TableRow>
          )}
          {filteredRows.map((r) => (
            <TableRow key={r.id}>
              <TableCell className="text-xs text-muted-foreground">
                {new Date(r.created_at).toLocaleString("pt-BR")}
              </TableCell>
              <TableCell className="font-medium">
                {r.stacks?.slug ?? <span className="text-muted-foreground">—</span>}
              </TableCell>
              <TableCell>{r.accounts?.name ?? "—"}</TableCell>
              <TableCell className="font-mono text-xs">
                {r.api_keys?.key_prefix ? `${r.api_keys.key_prefix}…` : "—"}
              </TableCell>
              <TableCell className="font-mono text-xs">{r.path}</TableCell>
              <TableCell className="text-xs text-muted-foreground">{r.model ?? "—"}</TableCell>
              <TableCell>
                {r.status_code < 400 ? (
                  <Badge variant="secondary">{r.status_code}</Badge>
                ) : (
                  <Badge variant="destructive">{r.status_code}</Badge>
                )}
              </TableCell>
              <TableCell>
                {r.stream ? <Badge variant="outline">sim</Badge> : <Badge variant="ghost">não</Badge>}
              </TableCell>
              <TableCell className="text-right font-mono text-xs tabular-nums">
                {r.tokens_in ?? "—"} / {r.tokens_out ?? "—"}
              </TableCell>
              <TableCell className="text-right font-mono text-xs tabular-nums">
                {r.duration_ms != null ? `${r.duration_ms}ms` : "—"}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}
