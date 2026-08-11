"use client"

import * as React from "react"
import { MonitorSmartphone, Unplug } from "lucide-react"
import { toast } from "sonner"

import { listStackClients, releaseStackClient } from "@/lib/actions"
import {
  CLIENT_WINDOW_DAYS,
  MAX_CLIENTS_BY_PLAN,
  type StackClient,
  type TemplatePlan,
} from "@/lib/types"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { RequestOriginBadge } from "@/components/dashboard/request-origin-badge"

export function StackClientsDialog({
  stackId,
  stackSlug,
  plan,
  open,
  onOpenChange,
}: {
  stackId: string
  stackSlug: string
  plan: TemplatePlan
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  // Diferente do KnowledgeFilesDialog, os ambientes NÃO vêm pré-carregados do
  // server component: stack_clients cresce com o uso e a página de stacks
  // carregaria a tabela inteira para preencher um dialog que quase nunca é
  // aberto. Busca sob demanda, com o remount por key (stack-row-actions.tsx)
  // garantindo dados frescos a cada abertura.
  const [clients, setClients] = React.useState<StackClient[] | null>(null)
  const [pending, startTransition] = React.useTransition()

  React.useEffect(() => {
    if (!open) return
    let cancelled = false
    listStackClients(stackId)
      .then((rows) => {
        if (!cancelled) setClients(rows)
      })
      .catch((e) => {
        if (!cancelled) {
          setClients([])
          toast.error(e instanceof Error ? e.message : "Erro ao listar ambientes")
        }
      })
    return () => {
      cancelled = true
    }
  }, [open, stackId])

  function onRelease(id: string) {
    startTransition(async () => {
      try {
        await releaseStackClient(id)
        setClients((prev) => (prev ?? []).filter((c) => c.id !== id))
        toast.success("Vaga liberada")
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "Erro ao liberar a vaga")
      }
    })
  }

  const limit = MAX_CLIENTS_BY_PLAN[plan]
  const used = clients?.length ?? 0
  const atLimit = limit !== null && used >= limit

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Ambientes conectados — {stackSlug}</DialogTitle>
          <DialogDescription>
            Lugares distintos que usaram esta stack nos últimos{" "}
            {CLIENT_WINDOW_DAYS} dias. Um ambiente é a combinação de ferramenta
            com bloco de rede, então o mesmo dev em casa e no escritório conta
            duas vezes, e uma equipe atrás da mesma rede conta uma. Ambiente
            sem uso na janela libera a vaga sozinho.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4">
          <p className="text-sm">
            <span className={atLimit ? "font-medium text-destructive" : "font-medium"}>
              {used}
              {limit === null ? "" : ` / ${limit}`}
            </span>{" "}
            <span className="text-muted-foreground">
              {limit === null
                ? `ambiente(s) — plano ${plan} não tem limite`
                : `ambiente(s) do plano ${plan}`}
            </span>
          </p>

          {clients === null && (
            <p className="text-sm text-muted-foreground">Carregando…</p>
          )}
          {clients !== null && clients.length === 0 && (
            <p className="text-sm text-muted-foreground">
              Nenhum ambiente registrado ainda. A stack passa a aparecer aqui
              no primeiro request que o gateway atender.
            </p>
          )}

          <div className="flex flex-col gap-2">
            {(clients ?? []).map((c) => (
              <div
                key={c.id}
                className="flex items-center justify-between gap-3 rounded-md border px-3 py-2 text-sm"
              >
                <div className="flex min-w-0 flex-col gap-1">
                  <span className="flex items-center gap-2">
                    {c.user_agent ? (
                      <RequestOriginBadge path="" userAgent={c.user_agent} />
                    ) : (
                      <span className="flex items-center gap-1 text-muted-foreground">
                        <MonitorSmartphone className="size-4" />
                        {c.client_label ?? "Sem identificação"}
                      </span>
                    )}
                    <span className="font-mono text-xs text-muted-foreground">
                      {c.ip_bucket ?? "rede desconhecida"}
                    </span>
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {c.requests.toLocaleString("pt-BR")} requisição(ões) · desde{" "}
                    {new Date(c.first_seen_at).toLocaleString("pt-BR")} · último
                    uso {new Date(c.last_seen_at).toLocaleString("pt-BR")}
                  </span>
                </div>
                <Button
                  variant="ghost"
                  size="icon"
                  disabled={pending}
                  onClick={() => onRelease(c.id)}
                  aria-label={`Liberar a vaga de ${c.client_label ?? c.fingerprint.slice(0, 8)}`}
                  title="Liberar a vaga deste ambiente"
                >
                  <Unplug className="size-4" />
                </Button>
              </div>
            ))}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
