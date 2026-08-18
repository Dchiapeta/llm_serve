"use client"

import * as React from "react"
import { usePathname, useRouter, useSearchParams } from "next/navigation"
import { Search } from "lucide-react"

import { TEMPLATE_PLANS } from "@/lib/types"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

const ALL = "__all__"

const COBRANCA_OPTIONS = [
  { value: "active", label: "Em dia" },
  { value: "trialing", label: "Trial" },
  { value: "past_due", label: "Em atraso" },
  { value: "suspended", label: "Suspensa" },
  { value: "canceled", label: "Cancelada" },
]

/**
 * Filtros na URL para o link ser compartilhável. `periodo` e `escopo` mudam a
 * janela das queries no servidor; busca, plano e cobrança são aplicados em
 * memória — com 26 contas, refazer a query por tecla digitada seria desperdício.
 * Se a base crescer para centenas, mover `q` para o servidor com .ilike().
 *
 * `escopo` não tem controle na barra: a página é sempre a lista de clientes.
 * Continua lido da URL (?escopo=todos|internos) para quem precisar do resto.
 */
export function CrmToolbar({
  q,
  plano,
  cobranca,
  actions,
}: {
  q: string
  plano: string
  cobranca: string
  actions?: React.ReactNode
}) {
  const router = useRouter()
  const pathname = usePathname()
  const searchParams = useSearchParams()
  const [term, setTerm] = React.useState(q)

  const commit = React.useCallback(
    (key: string, value: string) => {
      const params = new URLSearchParams(searchParams.toString())
      if (!value || value === ALL) params.delete(key)
      else params.set(key, value)
      router.replace(`${pathname}?${params.toString()}`, { scroll: false })
    },
    [router, pathname, searchParams]
  )

  // Debounce para não empilhar uma entrada no histórico por tecla digitada.
  React.useEffect(() => {
    if (term === q) return
    const id = setTimeout(() => commit("q", term), 250)
    return () => clearTimeout(id)
  }, [term, q, commit])

  return (
    <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
      <div className="relative sm:max-w-xs sm:flex-1">
        <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={term}
          onChange={(e) => setTerm(e.target.value)}
          placeholder="Buscar por nome, e-mail ou slug"
          className="pl-8"
        />
      </div>

      <Select
        value={plano || ALL}
        onValueChange={(v) => commit("plano", v)}
      >
        <SelectTrigger size="sm" className="w-full sm:w-36">
          <SelectValue placeholder="Plano" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ALL}>Todos os planos</SelectItem>
          {TEMPLATE_PLANS.map((p) => (
            <SelectItem key={p} value={p}>
              {p}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      <Select
        value={cobranca || ALL}
        onValueChange={(v) => commit("cobranca", v)}
      >
        <SelectTrigger size="sm" className="w-full sm:w-40">
          <SelectValue placeholder="Cobrança" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ALL}>Toda cobrança</SelectItem>
          {COBRANCA_OPTIONS.map((o) => (
            <SelectItem key={o.value} value={o.value}>
              {o.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      {actions && (
        <div className="flex items-center gap-2 sm:ml-auto">{actions}</div>
      )}
    </div>
  )
}
