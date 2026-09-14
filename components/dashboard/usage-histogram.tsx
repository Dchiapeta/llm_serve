"use client"

import { Bar, BarChart, CartesianGrid, XAxis } from "recharts"

import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart"

// Duas séries no config (não uma): a geração de imagem não produz token, então
// um período com tráfego dos dois tipos precisa das duas cores disponíveis
// mesmo mostrando uma barra por vez — ver `unit` abaixo.
const CONFIG: ChartConfig = {
  tokens: { label: "Tokens", color: "var(--chart-1)" },
  images: { label: "Imagens", color: "var(--chart-2)" },
}

const UNIT_LABEL: Record<"tokens" | "images", string> = {
  tokens: "tokens",
  images: "imagens",
}

export function UsageHistogram({
  data,
  unit = "tokens",
}: {
  data: { label: string; tokens: number; images: number }[]
  /** Qual série mostrar — a página não soma tokens com imagens no mesmo
   *  eixo, unidades diferentes não compartilham escala. Escolhida por
   *  UsageDistribution, que decide sozinha quando há as duas. */
  unit?: "tokens" | "images"
}) {
  const total = data.reduce((s, d) => s + d[unit], 0)

  if (total === 0) {
    return (
      <p className="py-10 text-center text-sm text-muted-foreground">
        Sem métricas de uso ainda.
      </p>
    )
  }

  return (
    <ChartContainer config={CONFIG} className="aspect-auto h-64 w-full">
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
                  {Number(value).toLocaleString("pt-BR")} {UNIT_LABEL[unit]}
                </span>
              )}
            />
          }
        />
        <Bar
          dataKey={unit}
          fill={`var(--color-${unit})`}
          radius={[4, 4, 0, 0]}
        />
      </BarChart>
    </ChartContainer>
  )
}
