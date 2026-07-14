"use client"

import { Info } from "lucide-react"

import type { Account, RoutingState } from "@/lib/types"
import { Badge } from "@/components/reui/badge"
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog"

export function ContaInfoDialog({
  account,
  route,
  currentMachineName,
  open,
  onOpenChange,
}: {
  account: Account
  route: RoutingState | undefined
  currentMachineName: string | undefined
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-xs">
        <DialogTitle className="sr-only">Informações da conta {account.name}</DialogTitle>
        <div className="text-muted-foreground -m-4 mb-0 flex items-center gap-2 border-b px-4 py-3 [&_svg]:size-4">
          <Info aria-hidden="true" />
          <span className="text-foreground text-sm font-medium">{account.name}</span>
        </div>

        <dl className="grid grid-cols-2 gap-y-2 text-sm">
          <dt className="text-muted-foreground">E-mail</dt>
          <dd>{account.email ?? "—"}</dd>

          <dt className="text-muted-foreground">Plano</dt>
          <dd>
            <Badge variant="secondary" size="sm">
              {account.plan}
            </Badge>
          </dd>

          <dt className="text-muted-foreground">Máquina atual</dt>
          <dd>{currentMachineName ?? "—"}</dd>

          <dt className="text-muted-foreground">Status do adapter</dt>
          <dd>{route?.lora_status ?? "unloaded"}</dd>

          <dt className="text-muted-foreground">Último uso</dt>
          <dd>
            {route?.last_used_at
              ? new Date(route.last_used_at).toLocaleString("pt-BR")
              : "—"}
          </dd>

          <dt className="text-muted-foreground">Conta criada em</dt>
          <dd>{new Date(account.created_at).toLocaleString("pt-BR")}</dd>
        </dl>

        {account.system_prompt && (
          <div className="text-sm">
            <p className="text-muted-foreground mb-1">System prompt</p>
            <p className="max-h-32 overflow-y-auto rounded-md border p-2 whitespace-pre-wrap">
              {account.system_prompt}
            </p>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
