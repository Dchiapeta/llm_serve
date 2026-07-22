"use client"

import { usePathname, useRouter, useSearchParams } from "next/navigation"

import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"

// Granularidade das barras do gráfico de custo. Escreve em ?bucket= — mesma
// mecânica do PeriodSwitch (searchParam + router.push), para o estado do filtro
// viver na URL e o server component recalcular.
export function GranularitySwitch({
  granularity,
  hourDisabled = false,
}: {
  granularity: "hour" | "day"
  /** períodos longos geram buckets demais por hora — o body força "day" */
  hourDisabled?: boolean
}) {
  const router = useRouter()
  const pathname = usePathname()
  const searchParams = useSearchParams()

  function handleChange(value: string) {
    if (!value) return
    const params = new URLSearchParams(searchParams.toString())
    params.set("bucket", value)
    router.push(`${pathname}?${params.toString()}`)
  }

  const hourItem = (
    <ToggleGroupItem value="hour" disabled={hourDisabled}>
      Hora
    </ToggleGroupItem>
  )

  return (
    <ToggleGroup
      type="single"
      variant="outline"
      size="sm"
      value={granularity}
      onValueChange={handleChange}
    >
      {hourDisabled ? (
        <Tooltip>
          {/* span: o trigger precisa de um nó que receba eventos — item
              desabilitado não dispara hover */}
          <TooltipTrigger asChild>
            <span>{hourItem}</span>
          </TooltipTrigger>
          <TooltipContent>
            Disponível apenas nos períodos de 24 horas e 7 dias
          </TooltipContent>
        </Tooltip>
      ) : (
        hourItem
      )}
      <ToggleGroupItem value="day">Dia</ToggleGroupItem>
    </ToggleGroup>
  )
}
