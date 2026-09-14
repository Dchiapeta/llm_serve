"use client"

import * as React from "react"

import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { UsageDonut } from "@/components/dashboard/usage-donut"
import { UsageHistogram } from "@/components/dashboard/usage-histogram"

export function UsageDistribution({
  donutData,
  histogramData,
}: {
  donutData: { name: string; requests: number }[]
  histogramData: { label: string; tokens: number; images: number }[]
}) {
  const [view, setView] = React.useState<"machine" | "histogram">("machine")
  const [unit, setUnit] = React.useState<"tokens" | "images">("tokens")

  const totalTokens = histogramData.reduce((s, d) => s + d.tokens, 0)
  const totalImages = histogramData.reduce((s, d) => s + d.images, 0)
  // Seletor só aparece quando as DUAS unidades têm dado no período — página
  // global mistura stack de LLM e de imagem, e um seletor sempre visível
  // ficaria travado numa unidade vazia na maioria dos períodos de hoje.
  const showUnitToggle = totalTokens > 0 && totalImages > 0
  // Com só uma unidade tendo dado, mostra ela direto — nunca a vazia, mesmo
  // que `unit` ainda esteja no valor default.
  const effectiveUnit =
    totalTokens === 0 && totalImages > 0 ? "images" : unit === "images" && totalImages === 0 ? "tokens" : unit

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <ToggleGroup
          type="single"
          variant="outline"
          size="sm"
          value={view}
          onValueChange={(v) => {
            if (v) setView(v as "machine" | "histogram")
          }}
        >
          <ToggleGroupItem value="machine">Por máquina</ToggleGroupItem>
          <ToggleGroupItem value="histogram">Histograma</ToggleGroupItem>
        </ToggleGroup>

        {view === "histogram" && showUnitToggle && (
          <ToggleGroup
            type="single"
            variant="outline"
            size="sm"
            value={effectiveUnit}
            onValueChange={(v) => {
              if (v) setUnit(v as "tokens" | "images")
            }}
          >
            <ToggleGroupItem value="tokens">Tokens</ToggleGroupItem>
            <ToggleGroupItem value="images">Imagens</ToggleGroupItem>
          </ToggleGroup>
        )}
      </div>

      {view === "machine" ? (
        <UsageDonut data={donutData} />
      ) : (
        <UsageHistogram data={histogramData} unit={effectiveUnit} />
      )}
    </div>
  )
}
