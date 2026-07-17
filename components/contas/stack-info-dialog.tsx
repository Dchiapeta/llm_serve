"use client"

import * as React from "react"
import Link from "next/link"
import { Copy, ExternalLink, Info } from "lucide-react"
import { toast } from "sonner"

import { Badge } from "@/components/reui/badge"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog"
import {
  PLAN_BADGE_VARIANT,
  formatPurchaseDate,
  type StackInfo,
} from "@/components/contas/contas-table"

export function StackInfoDialog({
  stack,
  periodLabel,
  open,
  onOpenChange,
}: {
  stack: StackInfo
  periodLabel: string
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const machine = stack.machine

  // Copia a chave completa quando ela existe (chaves criadas após a
  // migration 0014); chaves antigas só têm o prefixo recuperável.
  function copyKey(key: { plain_key: string | null; key_prefix: string }) {
    if (key.plain_key) {
      navigator.clipboard.writeText(key.plain_key)
      toast.success("Chave copiada")
    } else {
      navigator.clipboard.writeText(key.key_prefix)
      toast.success("Chave antiga sem texto completo — prefixo copiado")
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="sm:max-w-sm">
          <DialogTitle className="sr-only">
            Informações da stack {stack.slug}
          </DialogTitle>
          <div className="text-muted-foreground -m-4 mb-0 flex items-center gap-2 border-b px-4 py-3 [&_svg]:size-4">
            <Info aria-hidden="true" />
            <span className="text-foreground text-sm font-medium">
              {stack.slug}
            </span>
          </div>

          <div className="space-y-4">
            <dl className="grid grid-cols-2 gap-y-2 text-sm">
              <dt className="text-muted-foreground">Produto</dt>
              <dd>
                <Badge variant={PLAN_BADGE_VARIANT[stack.plan]} size="sm">
                  {stack.plan}
                </Badge>
              </dd>

              <dt className="text-muted-foreground">Subdomínio</dt>
              <dd>
                <code className="font-mono text-xs">{stack.slug}</code>
              </dd>

              <dt className="text-muted-foreground">Data da compra</dt>
              <dd>{formatPurchaseDate(stack.purchase_date)}</dd>

              <dt className="text-muted-foreground">Criada em</dt>
              <dd>{new Date(stack.created_at).toLocaleString("pt-BR")}</dd>

              <dt className="text-muted-foreground">Máquina</dt>
              <dd>
                {machine ? (
                  <Link
                    href={`/machines/${machine.id}`}
                    className="hover:underline"
                  >
                    {machine.name}
                  </Link>
                ) : (
                  "—"
                )}
              </dd>

              <dt className="text-muted-foreground">GPU</dt>
              <dd>
                {machine
                  ? `${machine.gpu_type}${machine.vram_gb ? ` · ${machine.vram_gb} GB` : ""}`
                  : "—"}
              </dd>

              <dt className="text-muted-foreground">Status da máquina</dt>
              <dd>{machine?.status ?? "—"}</dd>

              <dt className="text-muted-foreground">Modelo</dt>
              <dd
                className="truncate font-mono text-xs"
                title={machine?.model_name ?? undefined}
              >
                {machine?.model_name ?? "—"}
              </dd>

              <dt className="text-muted-foreground">Template</dt>
              <dd>{stack.templateName ?? "—"}</dd>

              <dt className="text-muted-foreground">Custo/h</dt>
              <dd>
                {machine?.cost_per_hr
                  ? `$${machine.cost_per_hr.toFixed(2)}`
                  : "—"}
              </dd>
            </dl>

            <div className="text-sm">
              <p className="text-muted-foreground mb-1">Chaves de API</p>
              {stack.keys.length === 0 ? (
                <p>—</p>
              ) : (
                <ul className="space-y-1">
                  {stack.keys.map((key) => (
                    <li
                      key={`${key.key_prefix}-${key.created_at}`}
                      className="flex items-center gap-2"
                    >
                      <code className="font-mono text-xs">
                        {key.key_prefix}…
                      </code>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="size-6 text-muted-foreground"
                        onClick={() => copyKey(key)}
                        aria-label={`Copiar chave ${key.key_prefix}`}
                      >
                        <Copy className="size-3" />
                      </Button>
                      <Badge
                        variant={
                          key.status === "active" ? "success-light" : "secondary"
                        }
                        size="sm"
                      >
                        {key.status}
                      </Badge>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <div className="text-sm">
              <p className="text-muted-foreground mb-1">Uso ({periodLabel})</p>
              <dl className="grid grid-cols-2 gap-y-2">
                <dt className="text-muted-foreground">Tokens (entrada)</dt>
                <dd>{stack.usage.tokensIn.toLocaleString("pt-BR")}</dd>

                <dt className="text-muted-foreground">Tokens (saída)</dt>
                <dd>{stack.usage.tokensOut.toLocaleString("pt-BR")}</dd>

                <dt className="text-muted-foreground">Requests</dt>
                <dd>{stack.usage.requests.toLocaleString("pt-BR")}</dd>
              </dl>
            </div>

            {stack.system_prompt && (
              <div className="text-sm">
                <p className="text-muted-foreground mb-1">System prompt</p>
                <p className="max-h-32 overflow-y-auto rounded-md border p-2 whitespace-pre-wrap">
                  {stack.system_prompt}
                </p>
              </div>
            )}

            {machine?.public_url && (
              <a
                href={machine.public_url}
                target="_blank"
                rel="noreferrer"
                className="text-primary inline-flex items-center gap-1 text-xs font-medium hover:underline"
              >
                <ExternalLink aria-hidden="true" className="size-2.5 shrink-0" />
                Abrir endpoint
              </a>
            )}
          </div>
      </DialogContent>
    </Dialog>
  )
}
