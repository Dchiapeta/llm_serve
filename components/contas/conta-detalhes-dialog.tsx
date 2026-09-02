"use client"

import { Info } from "lucide-react"

import { BILLING_BADGE } from "@/lib/billing-status"
import { PLAN_BADGE_VARIANT } from "@/lib/plan-badge"
import { Badge } from "@/components/reui/badge"
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog"
import type { UsuarioRow } from "@/components/contas/usuarios-table"

// Info da conta vista de /contas. Arquivo separado do conta-info-dialog.tsx:
// aquele é o "Info da conta" aberto de dentro de uma linha de /stacks e
// depende do routing_state e da máquina atual da stack, que esta página não
// carrega. Aqui o contexto é o inverso — a conta e as stacks que ela tem.
export function ContaDetalhesDialog({
  conta,
  open,
  onOpenChange,
}: {
  conta: UsuarioRow
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogTitle className="sr-only">
          Informações da conta {conta.name}
        </DialogTitle>
        <div className="text-muted-foreground -m-4 mb-0 flex items-center gap-2 border-b px-4 py-3 [&_svg]:size-4">
          <Info aria-hidden="true" />
          <span className="text-foreground text-sm font-medium">
            {conta.name}
          </span>
        </div>

        <div className="space-y-4">
          <dl className="grid grid-cols-2 gap-y-2 text-sm">
            <dt className="text-muted-foreground">ID</dt>
            <dd>
              <code className="block truncate font-mono text-xs" title={conta.id}>
                {conta.id}
              </code>
            </dd>

            <dt className="text-muted-foreground">E-mail</dt>
            <dd className="truncate" title={conta.email ?? undefined}>
              {conta.email ?? "—"}
            </dd>

            <dt className="text-muted-foreground">Login</dt>
            <dd>
              {conta.userId ? (
                <code
                  className="block truncate font-mono text-xs"
                  title={conta.userId}
                >
                  {conta.userId}
                </code>
              ) : (
                <span className="text-muted-foreground">sem login vinculado</span>
              )}
            </dd>

            <dt className="text-muted-foreground">Criada em</dt>
            <dd>{new Date(conta.createdAt).toLocaleString("pt-BR")}</dd>

            <dt className="text-muted-foreground">Tokens</dt>
            <dd className="tabular-nums">{conta.tokens.toLocaleString("pt-BR")}</dd>

            <dt className="text-muted-foreground">Requests</dt>
            <dd className="tabular-nums">
              {conta.requests.toLocaleString("pt-BR")}
            </dd>
          </dl>

          <div className="space-y-2 border-t pt-3">
            <p className="text-muted-foreground text-xs font-medium">
              {conta.stackList.length === 0
                ? "Nenhuma stack"
                : `${conta.stackList.length} stack(s)`}
            </p>
            {conta.stackList.map((stack) => {
              const billing = BILLING_BADGE[stack.billingStatus]
              return (
                <div
                  key={stack.id}
                  className="flex items-start justify-between gap-2 text-sm"
                >
                  <div className="min-w-0">
                    <p className="truncate font-medium">{stack.name}</p>
                    <code className="text-muted-foreground block truncate font-mono text-xs">
                      {stack.slug}
                    </code>
                    <p className="text-muted-foreground text-xs">
                      {stack.machineName ?? "Desativada"}
                    </p>
                  </div>
                  <div className="flex shrink-0 flex-col items-end gap-1">
                    <Badge variant={PLAN_BADGE_VARIANT[stack.plan]} size="sm">
                      {stack.plan}
                    </Badge>
                    {billing && (
                      <Badge variant={billing.variant} size="sm">
                        {billing.label}
                      </Badge>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
