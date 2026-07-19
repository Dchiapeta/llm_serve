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
  gatewayUrl,
  modelName,
}: {
  gatewayUrl: string | null
  modelName: string | null
}) {
  // Sempre o gateway, nunca o proxy do pod: o pod muda/pausa e o cliente não
  // pode saber disso — realocação e auto-wake só funcionam via gateway.
  // Fallback é a URL real de produção (Railway) — GATEWAY_URL pode não estar
  // setado no ambiente do painel, e um placeholder deixaria o snippet inútil.
  const url =
    gatewayUrl?.replace(/\/$/, "") ?? "https://llmserve-docker.up.railway.app"
  const model = modelName ?? "<modelo>"

  const curlSnippet = `curl ${url}/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer <SUA_CHAVE_DE_ACESSO>" \\
  -d '{
    "model": "${model}",
    "max_tokens": 8000,
    "messages": [{"role": "user", "content": "oi"}]
  }'`

  // AUTO_COMPACT_WINDOW: o Claude Code assume janela de 200k e não tem como
  // saber a real do plano (64k) — sem isso ele só descobre o limite quando o
  // gateway recusa; com isso ele compacta sozinho antes de estourar.
  const claudeSnippet = `export ANTHROPIC_BASE_URL="${url}"
export ANTHROPIC_AUTH_TOKEN="<SUA_CHAVE_DE_ACESSO>"
export ANTHROPIC_API_KEY=""
export ANTHROPIC_MODEL="${model}"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$ANTHROPIC_MODEL"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$ANTHROPIC_MODEL"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$ANTHROPIC_MODEL"
export CLAUDE_CODE_AUTO_COMPACT_WINDOW=50000
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
