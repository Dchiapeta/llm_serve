"use client"

import * as React from "react"
import Link from "next/link"
import { Search } from "lucide-react"

import type { ProvisionDecision, TemplatePlan } from "@/lib/types"
import { actorKind, causeLabel } from "@/components/machines/cause-labels"
import { Badge } from "@/components/ui/badge"
import { ActorBadge } from "@/components/machines/actor-badge"
import { CauseBadge } from "@/components/machines/cause-badge"
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

// Mesma forma do RequestRow (requests-table.tsx): os joins vêm da query da
// página; FKs nullable (set null na 0070) — o histórico sobrevive à stack/
// chave/máquina sumir, e a coluna mostra "—".
export type DecisionRow = ProvisionDecision & {
  stacks: { slug: string; plan: TemplatePlan } | null
  api_keys: { key_prefix: string } | null
  accounts: { name: string } | null
  machines: { name: string } | null
}

const OUTCOME_BADGE: Record<ProvisionDecision["outcome"], string> = {
  granted: "bg-emerald-100! text-emerald-700! dark:bg-emerald-950! dark:text-emerald-300!",
  denied: "bg-rose-100! text-rose-700! dark:bg-rose-950! dark:text-rose-300!",
  served_503: "bg-orange-100! text-orange-700! dark:bg-orange-950! dark:text-orange-300!",
}

const OUTCOME_LABEL: Record<ProvisionDecision["outcome"], string> = {
  granted: "concedida",
  denied: "negada",
  served_503: "503 servido",
}

const PER_PAGE_OPTIONS = [10, 20, 50, 100]

// Mesmo pageWindow de requests-table.tsx (janela ao redor da página atual).
function pageWindow(current: number, total: number): Array<number | "…"> {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1)
  const pages = new Set([1, total, current, current - 1, current + 1])
  const visible = [...pages].filter((p) => p >= 1 && p <= total).sort((a, b) => a - b)
  const out: Array<number | "…"> = []
  for (const [i, p] of visible.entries()) {
    if (i > 0 && p - (visible[i - 1] as number) > 1) out.push("…")
    out.push(p)
  }
  return out
}

export function DecisionsTable({ rows }: { rows: DecisionRow[] }) {
  const [query, setQuery] = React.useState("")
  const [outcomeFilter, setOutcomeFilter] = React.useState(ALL)
  const [kindFilter, setKindFilter] = React.useState(ALL)
  const [perPage, setPerPage] = React.useState(PER_PAGE_OPTIONS[1])
  const [page, setPage] = React.useState(1)

  const q = query.trim().toLowerCase()
  const filtered = rows.filter((r) => {
    const stackSlug = r.stacks?.slug ?? r.trigger_meta?.stack_slug ?? ""
    const keyPrefix = r.api_keys?.key_prefix ?? r.key_prefix ?? ""
    const matchesQuery = q
      ? stackSlug.toLowerCase().includes(q) ||
        (r.accounts?.name.toLowerCase().includes(q) ?? false) ||
        keyPrefix.toLowerCase().includes(q) ||
        (r.plan?.toLowerCase().includes(q) ?? false) ||
        r.cause.toLowerCase().includes(q) ||
        causeLabel(r.cause).toLowerCase().includes(q) ||
        (r.machines?.name.toLowerCase().includes(q) ?? false)
      : true
    const matchesOutcome = outcomeFilter === ALL || r.outcome === outcomeFilter
    const matchesKind = kindFilter === ALL || actorKind(r.actor) === kindFilter
    return matchesQuery && matchesOutcome && matchesKind
  })

  const totalPages = Math.max(Math.ceil(filtered.length / perPage), 1)
  const currentPage = Math.min(page, totalPages)
  const pageRows = filtered.slice((currentPage - 1) * perPage, currentPage * perPage)
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
            placeholder="Buscar por stack, conta, chave, plano, causa ou máquina…"
          />
        </InputGroup>

        <Select
          value={outcomeFilter}
          onValueChange={(v) => {
            setOutcomeFilter(v)
            setPage(1)
          }}
        >
          <SelectTrigger className="w-40">
            <SelectValue placeholder="Resultado" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>Todos os resultados</SelectItem>
            <SelectItem value="granted">Concedidas</SelectItem>
            <SelectItem value="denied">Negadas</SelectItem>
            <SelectItem value="served_503">503 servidos</SelectItem>
          </SelectContent>
        </Select>

        <Select
          value={kindFilter}
          onValueChange={(v) => {
            setKindFilter(v)
            setPage(1)
          }}
        >
          <SelectTrigger className="w-40">
            <SelectValue placeholder="Tipo" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>Todos os tipos</SelectItem>
            <SelectItem value="manual">Manual</SelectItem>
            <SelectItem value="request">Requisição</SelectItem>
            <SelectItem value="automatic">Automática</SelectItem>
          </SelectContent>
        </Select>
      </div>

      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Hora</TableHead>
            <TableHead>Tipo</TableHead>
            <TableHead>Resultado</TableHead>
            <TableHead>Causa</TableHead>
            <TableHead>Plano</TableHead>
            <TableHead>Stack</TableHead>
            <TableHead>Conta</TableHead>
            <TableHead>Chave</TableHead>
            <TableHead>Origem</TableHead>
            <TableHead>Máquina</TableHead>
            <TableHead>Motivo</TableHead>
            <TableHead className="text-right!">Repetições</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {filtered.length === 0 && (
            <TableRow>
              <TableCell colSpan={12} className="text-center text-muted-foreground">
                Nenhuma decisão encontrada.
              </TableCell>
            </TableRow>
          )}
          {pageRows.map((r) => (
            <TableRow key={r.id}>
              <TableCell className="text-xs text-muted-foreground">
                {new Date(r.created_at).toLocaleString("pt-BR")}
              </TableCell>
              <TableCell>
                <ActorBadge actor={r.actor} email={r.trigger_meta?.admin_email} />
              </TableCell>
              <TableCell>
                <Badge className={OUTCOME_BADGE[r.outcome]}>{OUTCOME_LABEL[r.outcome]}</Badge>
              </TableCell>
              <TableCell>
                <CauseBadge cause={r.cause} />
              </TableCell>
              <TableCell>
                <PlanBadge plan={(r.stacks?.plan ?? r.plan ?? undefined) as TemplatePlan | undefined} />
              </TableCell>
              <TableCell className="font-medium">
                {r.stacks?.slug ?? r.trigger_meta?.stack_slug ?? (
                  <span className="text-muted-foreground">—</span>
                )}
              </TableCell>
              <TableCell>{r.accounts?.name ?? r.trigger_meta?.account_name ?? "—"}</TableCell>
              <TableCell className="font-mono text-xs">
                {r.api_keys?.key_prefix ?? r.key_prefix
                  ? `${r.api_keys?.key_prefix ?? r.key_prefix}…`
                  : "—"}
              </TableCell>
              <TableCell>
                {r.trigger_meta?.path ? (
                  <RequestOriginBadge
                    path={r.trigger_meta.path}
                    userAgent={r.trigger_meta.user_agent ?? null}
                  />
                ) : (
                  <span className="text-xs text-muted-foreground">—</span>
                )}
              </TableCell>
              <TableCell>
                {r.machine_id ? (
                  <Link href={`/machines/${r.machine_id}`} className="hover:underline">
                    {r.machines?.name ?? r.trigger_meta?.machine_name ?? r.machine_id.slice(0, 8)}
                  </Link>
                ) : (
                  <span className="text-muted-foreground">—</span>
                )}
              </TableCell>
              {/* mensagem crua (ex.: a recusa do RunPod) — truncada na célula,
                  inteira no tooltip */}
              <TableCell
                className="max-w-64 truncate text-xs text-muted-foreground"
                title={r.trigger_meta?.reason ?? undefined}
              >
                {r.trigger_meta?.reason ?? "—"}
              </TableCell>
              <TableCell className="text-right font-mono text-xs tabular-nums">
                {r.repeat_count}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>

      <Pagination>
        <PaginationContent className="w-full justify-between">
          <PaginationItem>
            <span className="text-muted-foreground text-sm">
              Página <span className="text-foreground font-medium">{currentPage}</span> de{" "}
              <span className="text-foreground font-medium">{totalPages}</span> ·{" "}
              <span className="text-foreground font-medium">{filtered.length}</span>{" "}
              {filtered.length === 1 ? "decisão" : "decisões"}
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
              className={currentPage === totalPages ? "pointer-events-none opacity-50" : undefined}
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
              aria-label="Decisões por página"
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
