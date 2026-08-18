"use client"

import { Bar, BarChart, CartesianGrid, XAxis } from "recharts"

import { formatMoney } from "@/lib/chargefy-format"
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart"

// Série única. A paleta do projeto é monocromática de propósito (--chart-1..5
// são o mesmo cinza), então codificar categoria por cor não funcionaria aqui —
// a categoria vive no eixo X, que é onde ela se lê melhor de qualquer forma.
const config: ChartConfig = {
  mrr: { label: "MRR", color: "var(--chart-1)" },
}

export function MrrByPlanChart({
  data,
}: {
  data: { label: string; mrr: number }[]
}) {
  const total = data.reduce((s, d) => s + d.mrr, 0)

  if (total === 0) {
    return (
      <p className="py-10 text-center text-sm text-muted-foreground">
        Sem receita registrada no período.
      </p>
    )
  }

  return (
    <ChartContainer config={config} className="aspect-auto h-64 w-full">
      <BarChart data={data} margin={{ top: 8, right: 8, left: 8, bottom: 0 }}>
        <CartesianGrid vertical={false} />
        <XAxis
          dataKey="label"
          tickLine={false}
          axisLine={false}
          tickMargin={8}
        />
        <ChartTooltip
          cursor={false}
          content={
            <ChartTooltipContent
              formatter={(value) => (
                <span className="font-mono font-medium tabular-nums">
                  {formatMoney(Number(value))}
                </span>
              )}
            />
          }
        />
        <Bar dataKey="mrr" fill="var(--color-mrr)" radius={[4, 4, 0, 0]} />
      </BarChart>
    </ChartContainer>
  )
}
