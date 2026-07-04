"use client"

import * as React from "react"
import { Label, Pie, PieChart } from "recharts"

import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart"

const palette = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
]

export function UsageDonut({
  data,
}: {
  data: { name: string; requests: number }[]
}) {
  const total = data.reduce((s, d) => s + d.requests, 0)

  const chartData = data.map((d, i) => ({
    ...d,
    fill: palette[i % palette.length],
  }))

  const config: ChartConfig = Object.fromEntries(
    data.map((d, i) => [d.name, { label: d.name, color: palette[i % palette.length] }])
  )

  if (total === 0) {
    return (
      <p className="py-10 text-center text-sm text-muted-foreground">
        Sem métricas de uso ainda.
      </p>
    )
  }

  return (
    <ChartContainer config={config} className="mx-auto aspect-square max-h-64">
      <PieChart>
        <ChartTooltip cursor={false} content={<ChartTooltipContent hideLabel />} />
        <Pie
          data={chartData}
          dataKey="requests"
          nameKey="name"
          innerRadius={60}
          strokeWidth={4}
        >
          <Label
            content={({ viewBox }) => {
              if (viewBox && "cx" in viewBox && "cy" in viewBox) {
                return (
                  <text
                    x={viewBox.cx}
                    y={viewBox.cy}
                    textAnchor="middle"
                    dominantBaseline="middle"
                  >
                    <tspan
                      x={viewBox.cx}
                      y={viewBox.cy}
                      className="fill-foreground text-2xl font-bold"
                    >
                      {total.toLocaleString("pt-BR")}
                    </tspan>
                    <tspan
                      x={viewBox.cx}
                      y={(viewBox.cy ?? 0) + 22}
                      className="fill-muted-foreground text-xs"
                    >
                      requisições
                    </tspan>
                  </text>
                )
              }
            }}
          />
        </Pie>
      </PieChart>
    </ChartContainer>
  )
}
