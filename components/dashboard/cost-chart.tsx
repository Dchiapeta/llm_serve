"use client"

import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from "recharts"

import { formatUsd, type CostBucket } from "@/lib/billing"
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart"

// Séries EMPILHADAS na mesma escala (US$): GPU gasta, OpenRouter e, por cima, a
// economia hachurada — o que as máquinas custariam a mais ligadas 24/7. Sem
// segundo eixo: é a mesma grandeza. O OpenRouter fica fora do 24/7 (é por
// requisição, não tem "ligado direto"), por isso a hachura sai só da GPU.
//
// A economia usa hachura, não uma segunda cor: o design system é monocromático
// (--chart-1..5 são tons de cinza, idênticos em light e dark), então diferenciar
// por textura sobrevive aos dois temas e ao daltonismo/impressão.
const config: ChartConfig = {
  spent: { label: "RunPod", color: "var(--chart-2)" },
  openrouter: { label: "OpenRouter", color: "var(--chart-4)" },
  saved: { label: "Economizado", color: "var(--muted-foreground)" },
}

export function CostChart({ data }: { data: CostBucket[] }) {
  const total = data.reduce((s, d) => s + d.baseline + d.openrouter, 0)

  if (total === 0) {
    return (
      <p className="py-10 text-center text-sm text-muted-foreground">
        Sem gasto no período — nem máquina ligada, nem requisição cobrada pelo
        OpenRouter.
      </p>
    )
  }

  const chartData = data.map((d) => ({
    label: d.label,
    spent: d.spent,
    openrouter: d.openrouter,
    saved: Math.max(0, d.baseline - d.spent),
  }))

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-4 text-xs text-muted-foreground">
        <span className="flex items-center gap-1.5">
          <span
            className="size-2.5 rounded-[2px]"
            style={{ background: "var(--chart-2)" }}
          />
          RunPod
        </span>
        <span className="flex items-center gap-1.5">
          <span
            className="size-2.5 rounded-[2px]"
            style={{ background: "var(--chart-4)" }}
          />
          OpenRouter
        </span>
        <span className="flex items-center gap-1.5">
          <span
            className="size-2.5 rounded-[2px] border border-border"
            style={{
              background:
                "repeating-linear-gradient(45deg, var(--muted-foreground) 0 1px, transparent 1px 3px)",
            }}
          />
          Economizado
        </span>
      </div>

      <ChartContainer config={config} className="aspect-auto h-64 w-full">
        <BarChart data={chartData} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
          <defs>
            <pattern
              id="cost-saved-hatch"
              width={6}
              height={6}
              patternTransform="rotate(45)"
              patternUnits="userSpaceOnUse"
            >
              <rect width={6} height={6} fill="transparent" />
              <line
                x1={0}
                y1={0}
                x2={0}
                y2={6}
                stroke="var(--muted-foreground)"
                strokeWidth={1.5}
              />
            </pattern>
          </defs>

          <CartesianGrid vertical={false} />
          <XAxis
            dataKey="label"
            tickLine={false}
            axisLine={false}
            tickMargin={8}
            minTickGap={16}
          />
          <YAxis
            tickLine={false}
            axisLine={false}
            width={52}
            tickFormatter={(v) => formatUsd(Number(v))}
          />
          <ChartTooltip
            cursor={false}
            content={
              <ChartTooltipContent
                labelFormatter={(label, payload) => {
                  const value = (key: string) =>
                    Number((payload ?? []).find((item) => item.dataKey === key)?.value ?? 0)
                  return (
                    <span>
                      {String(label)} · {formatUsd(value("spent") + value("openrouter"))}{" "}
                      <span className="text-muted-foreground">
                        · GPU 24/7 custaria {formatUsd(value("spent") + value("saved"))}
                      </span>
                    </span>
                  )
                }}
                formatter={(value, name) => (
                  <div className="flex flex-1 items-center justify-between gap-4 leading-none">
                    <span className="text-muted-foreground">
                      {config[name as keyof typeof config]?.label ?? name}
                    </span>
                    <span className="font-mono font-medium tabular-nums">
                      {formatUsd(Number(value))}
                    </span>
                  </div>
                )}
              />
            }
          />
          <Bar dataKey="spent" stackId="cost" fill="var(--color-spent)" />
          <Bar dataKey="openrouter" stackId="cost" fill="var(--color-openrouter)" />
          <Bar
            dataKey="saved"
            stackId="cost"
            fill="url(#cost-saved-hatch)"
            // 2px da cor da superfície separam a fatia hipotética da real
            stroke="var(--card)"
            strokeWidth={2}
            radius={[4, 4, 0, 0]}
          />
        </BarChart>
      </ChartContainer>
    </div>
  )
}
