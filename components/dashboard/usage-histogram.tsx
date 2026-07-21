"use client"

import { Bar, BarChart, CartesianGrid, XAxis } from "recharts"

import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart"

const config: ChartConfig = {
  tokens: { label: "Tokens", color: "var(--chart-1)" },
}

export function UsageHistogram({
  data,
}: {
  data: { label: string; tokens: number }[]
}) {
  const total = data.reduce((s, d) => s + d.tokens, 0)

  if (total === 0) {
    return (
      <p className="py-10 text-center text-sm text-muted-foreground">
        Sem métricas de uso ainda.
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
          minTickGap={16}
        />
        <ChartTooltip
          cursor={false}
          content={
            <ChartTooltipContent
              formatter={(value) => (
                <span className="font-mono font-medium tabular-nums">
                  {Number(value).toLocaleString("pt-BR")} tokens
                </span>
              )}
            />
          }
        />
        <Bar dataKey="tokens" fill="var(--color-tokens)" radius={[4, 4, 0, 0]} />
      </BarChart>
    </ChartContainer>
  )
}
