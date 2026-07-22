"use client"

import { ChevronDown } from "lucide-react"
import { usePathname, useRouter, useSearchParams } from "next/navigation"

import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"

export type PeriodOption = { value: string; label: string }

const DEFAULT_OPTIONS: readonly PeriodOption[] = [
  { value: "24h", label: "24 horas" },
  { value: "7d", label: "7 dias" },
  { value: "total", label: "Total" },
]

// `options` existe porque cada página tem seu próprio conjunto de períodos
// (o Financeiro acrescenta 30 dias). Não mexer no default: o PERIOD_MS do
// dashboard não conhece outros valores e cairia no fallback silencioso de 24h.
export function PeriodSwitch({
  period,
  options = DEFAULT_OPTIONS,
}: {
  period: string
  options?: readonly PeriodOption[]
}) {
  const router = useRouter()
  const pathname = usePathname()
  const searchParams = useSearchParams()

  const current = options.find((opt) => opt.value === period) ?? options[0]

  function handleChange(value: string) {
    const params = new URLSearchParams(searchParams.toString())
    params.set("period", value)
    router.push(`${pathname}?${params.toString()}`)
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" className="gap-1.5">
          {current.label}
          <ChevronDown className="size-3.5 text-muted-foreground" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuRadioGroup value={period} onValueChange={handleChange}>
          {options.map((opt) => (
            <DropdownMenuRadioItem key={opt.value} value={opt.value}>
              {opt.label}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
