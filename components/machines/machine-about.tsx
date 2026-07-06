"use client"

import { useState } from "react"
import { Check, Copy } from "lucide-react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"

function CodeBlock({ code }: { code: string }) {
  const [copied, setCopied] = useState(false)

  async function copy() {
    await navigator.clipboard.writeText(code)
    setCopied(true)
    toast.success("Copiado")
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div className="relative rounded-md border bg-muted/50">
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

export function MachineAbout({
  publicUrl,
  modelName,
}: {
  publicUrl: string | null
  modelName: string | null
}) {
  const url = publicUrl ?? "https://<url-da-maquina>"
  const model = modelName ?? "<modelo>"

  const curlSnippet = `curl ${url}/v1/messages \\
  -H "Content-Type: application/json" \\
  -H "anthropic-version: 2023-06-01" \\
  -d '{
    "model": "${model}",
    "max_tokens": 200,
    "messages": [{"role": "user", "content": "oi"}]
  }'`

  const claudeSnippet = `export ANTHROPIC_BASE_URL="${url}"
export ANTHROPIC_AUTH_TOKEN="ollama"
export ANTHROPIC_API_KEY=""
export ANTHROPIC_MODEL="${model}"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$ANTHROPIC_MODEL"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$ANTHROPIC_MODEL"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$ANTHROPIC_MODEL"
claude`

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h3 className="mb-2 text-sm font-medium">Terminal (curl)</h3>
        <CodeBlock code={curlSnippet} />
      </div>
      <div>
        <h3 className="mb-2 text-sm font-medium">Claude Code CLI</h3>
        <CodeBlock code={claudeSnippet} />
      </div>
    </div>
  )
}
