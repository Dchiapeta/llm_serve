"use client"

import * as React from "react"
import { RefreshCw } from "lucide-react"

import { Button } from "@/components/ui/button"
import { ScrollArea } from "@/components/ui/scroll-area"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

type KeyOption = { key_prefix: string; account_name: string }

export function MachineLogs({
  machineId,
  keys,
}: {
  machineId: string
  keys: KeyOption[]
}) {
  const [lines, setLines] = React.useState<string[]>([])
  const [error, setError] = React.useState<string | null>(null)
  const [filter, setFilter] = React.useState<string>("all")
  const [loading, setLoading] = React.useState(false)

  const load = React.useCallback(async () => {
    setLoading(true)
    try {
      const params = new URLSearchParams({ tail: "300" })
      if (filter !== "all") params.set("key_prefix", filter)
      const res = await fetch(`/api/machines/${machineId}/logs?${params}`)
      const data = await res.json()
      setLines(data.lines ?? [])
      setError(data.error ?? null)
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }, [machineId, filter])

  React.useEffect(() => {
    load()
    const interval = setInterval(load, 10_000)
    return () => clearInterval(interval)
  }, [load])

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-2">
        <Select value={filter} onValueChange={setFilter}>
          <SelectTrigger className="w-64">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">Máquina inteira</SelectItem>
            {keys.map((k) => (
              <SelectItem key={k.key_prefix} value={k.key_prefix}>
                {k.account_name} ({k.key_prefix}…)
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Button variant="outline" size="sm" onClick={load} disabled={loading}>
          <RefreshCw className={loading ? "size-4 animate-spin" : "size-4"} />
          Atualizar
        </Button>
      </div>

      {error && (
        <p className="text-xs text-amber-600 dark:text-amber-400">{error}</p>
      )}

      <ScrollArea className="h-96 rounded-lg border bg-zinc-950 p-4">
        <pre className="font-mono text-xs leading-relaxed text-zinc-100 whitespace-pre-wrap">
          {lines.length > 0 ? lines.join("\n") : "Sem logs ainda."}
        </pre>
      </ScrollArea>
    </div>
  )
}
