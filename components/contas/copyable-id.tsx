"use client"

import { Copy } from "lucide-react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"

// Exibe um id truncado com botão de copiar o valor completo — para uso em
// células de tabela de server components.
export function CopyableId({ value }: { value: string }) {
  return (
    <div className="flex items-center gap-1">
      <code className="font-mono text-xs" title={value}>
        {value.slice(0, 8)}…
      </code>
      <Button
        variant="ghost"
        size="icon"
        className="size-6"
        onClick={() => {
          navigator.clipboard.writeText(value)
          toast.success("ID copiado")
        }}
        aria-label={`Copiar ID ${value}`}
      >
        <Copy className="size-3" />
      </Button>
    </div>
  )
}
