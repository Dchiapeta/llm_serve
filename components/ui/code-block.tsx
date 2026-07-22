"use client"

import { useState } from "react"
import { Check, Copy } from "lucide-react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

/** Bloco de código com copiar. Sem syntax highlight de propósito: o projeto não
 * tem highlighter e os snippets são curtos — um <pre> monoespaçado basta e evita
 * arrastar Prism/Shiki (e o peso de client bundle) só por isso.
 * `label` identifica o arquivo/destino do snippet (ex.: "~/.codex/config.toml"),
 * que nas configs de ferramenta é informação load-bearing: o mesmo TOML colado
 * no arquivo errado não faz nada. */
export function CodeBlock({
  code,
  label,
  className,
}: {
  code: string
  label?: string
  className?: string
}) {
  const [copied, setCopied] = useState(false)

  async function copy() {
    await navigator.clipboard.writeText(code)
    setCopied(true)
    toast.success("Copiado")
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div className={cn("relative rounded-md border bg-muted/50", className)}>
      {label ? (
        <div className="border-b px-4 py-2 pr-12 font-mono text-xs text-muted-foreground">
          {label}
        </div>
      ) : null}
      <Button
        variant="ghost"
        size="icon"
        onClick={copy}
        className="absolute top-2 right-2 size-7"
        aria-label="Copiar"
      >
        {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
      </Button>
      <pre className="overflow-x-auto p-4 pr-12 font-mono text-xs leading-relaxed">
        {code}
      </pre>
    </div>
  )
}
