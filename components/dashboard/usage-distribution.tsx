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
  histogramData: { label: string; tokens: number }[]
}) {
  const [view, setView] = React.useState<"machine" | "histogram">("machine")

  return (
    <div className="space-y-4">
      <ToggleGroup
        type="single"
        variant="outline"
        size="sm"
        value={view}
        onValueChange={(v) => {
          if (v) setView(v as "machine" | "histogram")
        }}
        className="self-start"
      >
        <ToggleGroupItem value="machine">Por máquina</ToggleGroupItem>
        <ToggleGroupItem value="histogram">Histograma</ToggleGroupItem>
      </ToggleGroup>

      {view === "machine" ? (
        <UsageDonut data={donutData} />
      ) : (
        <UsageHistogram data={histogramData} />
      )}
    </div>
  )
}
