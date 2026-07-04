"use client"

import * as React from "react"
import { RefreshCw } from "lucide-react"
import { toast } from "sonner"

import { pollMetrics } from "@/lib/actions"
import { Button } from "@/components/ui/button"

export function PollMetricsButton() {
  const [pending, startTransition] = React.useTransition()

  // coleta automática a cada 60s enquanto o dashboard está aberto
  React.useEffect(() => {
    const interval = setInterval(() => {
      startTransition(() => pollMetrics().catch(() => {}))
    }, 60_000)
    return () => clearInterval(interval)
  }, [])

  return (
    <Button
      variant="outline"
      size="sm"
      disabled={pending}
      onClick={() =>
        startTransition(async () => {
          try {
            await pollMetrics()
            toast.success("Métricas coletadas")
          } catch (e) {
            toast.error(e instanceof Error ? e.message : "Falha na coleta")
          }
        })
      }
    >
      <RefreshCw className={pending ? "size-4 animate-spin" : "size-4"} />
      Coletar métricas
    </Button>
  )
}
