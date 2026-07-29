"use client"

import * as React from "react"
import { Search } from "lucide-react"

import type { GatewayRequest, TemplatePlan } from "@/lib/types"
import { requestOrigin } from "@/lib/request-origin"
import { Badge } from "@/components/ui/badge"
import { PlanBadge } from "@/components/machines/plan-badge"
import { RequestOriginBadge } from "@/components/dashboard/request-origin-badge"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupInput,
} from "@/components/ui/input-group"
import {
  NativeSelect,
  NativeSelectOption,
} from "@/components/ui/native-select"
import {
  Pagination,
  PaginationContent,
  PaginationEllipsis,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination"
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
  // plan vem da stack, não de gateway_requests: o plano é do contrato do
  // cliente, não da requisição. FK nullable (migration 0038) — stack apagada
  // depois deixa o histórico sem plano, e a coluna mostra "—".
  stacks: { slug: string; plan: TemplatePlan } | null
  api_keys: { key_prefix: string } | null
  accounts: { name: string } | null
}

// Requisições agênticas duram de segundos a minutos; milissegundos davam
// números de 6 dígitos que ninguém lê ("129640ms"). Uma casa decimal preserva
// a resolução útil (0,3s continua distinguível de 0,9s).
function formatDuration(ms: number | null): string {
  if (ms == null) return "—"
  return `${(ms / 1000).toLocaleString("pt-BR", { maximumFractionDigits: 1 })}s`
}

// Azul = streaming, roxo = resposta única. Sufixo `!` porque os estilos da
// ReUI (.style-nova .cn-badge-variant-*) têm especificidade maior que
// utilitários — mesmo motivo de components/machines/plan-badge.tsx.
const STREAM_BADGE = {
  sim: "bg-blue-100! text-blue-700! dark:bg-blue-950! dark:text-blue-300!",
  não: "bg-purple-100! text-purple-700! dark:bg-purple-950! dark:text-purple-300!",
}

const PER_PAGE_OPTIONS = [10, 20, 50, 100]

// Janela de páginas numeradas ao redor da atual. Com 200 linhas carregadas e
// 10 por página são 20 páginas — mostrar todas encheria a barra, então
// aparecem a primeira, a última, as vizinhas da atual e "…" no lugar do resto.
function pageWindow(current: number, total: number): Array<number | "…"> {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1)
  const pages = new Set([1, total, current, current - 1, current + 1])
  const visible = [...pages].filter((p) => p >= 1 && p <= total).sort((a, b) => a - b)
  const out: Array<number | "…"> = []
  for (const [i, p] of visible.entries()) {
    // só entra "…" quando há um salto real; entre 3 e 4 o gap é zero
    if (i > 0 && p - (visible[i - 1] as number) > 1) out.push("…")
    out.push(p)
  }
  return out
}

export function RequestsTable({ rows }: { rows: RequestRow[] }) {
  const [query, setQuery] = React.useState("")
  const [statusFilter, setStatusFilter] = React.useState(ALL)
  const [perPage, setPerPage] = React.useState(PER_PAGE_OPTIONS[1])
  const [page, setPage] = React.useState(1)

  const normalizedQuery = query.trim().toLowerCase()
  const filteredRows = rows.filter((r) => {
    const matchesQuery = normalizedQuery
      ? (r.stacks?.slug.toLowerCase().includes(normalizedQuery) ?? false) ||
        (r.accounts?.name.toLowerCase().includes(normalizedQuery) ?? false) ||
        (r.api_keys?.key_prefix.toLowerCase().includes(normalizedQuery) ?? false) ||
        (r.stacks?.plan.toLowerCase().includes(normalizedQuery) ?? false) ||
        // buscar por "claude" ou "codex" é o uso óbvio assim que a coluna
        // Origem existe — casa o rótulo, não o User-Agent cru
        requestOrigin({ path: r.path, userAgent: r.user_agent })
          .label.toLowerCase()
          .includes(normalizedQuery)
      : true
    const matchesStatus =
      statusFilter === ALL ||
      (statusFilter === "ok" ? r.status_code < 400 : r.status_code >= 400)
    return matchesQuery && matchesStatus
  })

  const totalPages = Math.max(Math.ceil(filteredRows.length / perPage), 1)
  // clamp em vez de useEffect: filtrar/mudar itens-por-página pode deixar a
  // página atual fora do range (estava na 12 de 20, filtrou e sobraram 3), e um
  // efeito que corrige depois renderizaria uma tabela vazia por um frame
  const currentPage = Math.min(page, totalPages)
  const pageRows = filteredRows.slice(
    (currentPage - 1) * perPage,
    currentPage * perPage
  )

  const goTo = (p: number) => setPage(Math.min(Math.max(p, 1), totalPages))

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <InputGroup className="max-w-xs">
          <InputGroupAddon>
            <Search className="size-4 text-muted-foreground" />
          </InputGroupAddon>
          <InputGroupInput
            value={query}
            onChange={(e) => {
              setQuery(e.target.value)
              setPage(1)
            }}
            placeholder="Buscar por stack, conta, chave, plano ou origem…"
          />
        </InputGroup>

        <Select
          value={statusFilter}
          onValueChange={(v) => {
            setStatusFilter(v)
            setPage(1)
          }}
        >
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
            <TableHead>Origem</TableHead>
            <TableHead>Endpoint</TableHead>
            <TableHead>Plano</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Stream</TableHead>
            {/* `text-right!`: .style-nova .cn-table-head aplica text-left com
                especificidade 0-2-0 e vence o utilitário sem o `!` — sem isso
                o cabeçalho fica à esquerda e os números à direita */}
            <TableHead className="text-right!">Tokens in/out</TableHead>
            <TableHead className="text-right!">Duração</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {filteredRows.length === 0 && (
            <TableRow>
              <TableCell colSpan={11} className="text-center text-muted-foreground">
                Nenhuma requisição encontrada.
              </TableCell>
            </TableRow>
          )}
          {pageRows.map((r) => (
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
              <TableCell>
                <RequestOriginBadge path={r.path} userAgent={r.user_agent} />
              </TableCell>
              <TableCell className="font-mono text-xs">{r.path}</TableCell>
              <TableCell>
                <PlanBadge plan={r.stacks?.plan} />
              </TableCell>
              <TableCell>
                {r.status_code < 400 ? (
                  <Badge variant="secondary">{r.status_code}</Badge>
                ) : (
                  <Badge variant="destructive">{r.status_code}</Badge>
                )}
              </TableCell>
              <TableCell>
                {r.stream ? (
                  <Badge className={STREAM_BADGE.sim}>sim</Badge>
                ) : (
                  <Badge className={STREAM_BADGE.não}>não</Badge>
                )}
              </TableCell>
              <TableCell className="text-right font-mono text-xs tabular-nums">
                {r.tokens_in ?? "—"} / {r.tokens_out ?? "—"}
              </TableCell>
              <TableCell className="text-right font-mono text-xs tabular-nums">
                {formatDuration(r.duration_ms)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>

      {/* Padrão c-pagination-15 do ReUI. Os href="#" + preventDefault vêm de
          manter o componente vendorado fiel ao upstream (ele tipa o link como
          <a>); a navegação é toda client-side sobre as linhas já carregadas. */}
      <Pagination>
        <PaginationContent className="w-full justify-between">
          <PaginationItem>
            <span className="text-muted-foreground text-sm">
              Página <span className="text-foreground font-medium">{currentPage}</span>{" "}
              de <span className="text-foreground font-medium">{totalPages}</span> ·{" "}
              <span className="text-foreground font-medium">{filteredRows.length}</span>{" "}
              {filteredRows.length === 1 ? "requisição" : "requisições"}
            </span>
          </PaginationItem>
          <PaginationItem className="flex items-center gap-1">
            <PaginationPrevious
              href="#"
              text="Anterior"
              aria-disabled={currentPage === 1}
              className={currentPage === 1 ? "pointer-events-none opacity-50" : undefined}
              onClick={(e) => {
                e.preventDefault()
                goTo(currentPage - 1)
              }}
            />
            {pageWindow(currentPage, totalPages).map((p, i) =>
              p === "…" ? (
                // índice na key porque "…" pode aparecer duas vezes na mesma barra
                <PaginationEllipsis key={`gap-${i}`} />
              ) : (
                <PaginationLink
                  key={p}
                  href="#"
                  isActive={p === currentPage}
                  onClick={(e) => {
                    e.preventDefault()
                    goTo(p)
                  }}
                >
                  {p}
                </PaginationLink>
              )
            )}
            <PaginationNext
              href="#"
              text="Próxima"
              aria-disabled={currentPage === totalPages}
              className={
                currentPage === totalPages ? "pointer-events-none opacity-50" : undefined
              }
              onClick={(e) => {
                e.preventDefault()
                goTo(currentPage + 1)
              }}
            />
          </PaginationItem>
          <PaginationItem>
            <NativeSelect
              className="w-28"
              value={perPage}
              aria-label="Requisições por página"
              onChange={(e) => {
                setPerPage(Number(e.target.value))
                setPage(1)
              }}
            >
              {PER_PAGE_OPTIONS.map((n) => (
                <NativeSelectOption key={n} value={n}>
                  {n} / página
                </NativeSelectOption>
              ))}
            </NativeSelect>
          </PaginationItem>
        </PaginationContent>
      </Pagination>
    </div>
  )
}
