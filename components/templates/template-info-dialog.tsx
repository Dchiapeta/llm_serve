"use client"

import * as React from "react"
import { ChevronDown, Info, Server } from "lucide-react"

import { getMachineInfoDetails, type MachineInfoDetails } from "@/lib/actions"
import { cn } from "@/lib/utils"
import type { Machine, Template } from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible"
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog"
import { Separator } from "@/components/ui/separator"
import { Spinner } from "@/components/ui/spinner"
import { StatusBadge } from "@/components/machines/status-badge"

type DetailsState = {
  loading: boolean
  data?: MachineInfoDetails
  error?: string
}

// "Tempo ligado" só faz sentido para máquina rodando; usa lastStartedAt do
// RunPod. Para status diferente de running (ou sem timestamp), mostra "—".
function formatUptime(
  status: Machine["status"],
  lastStartedAt: string | null | undefined
): string {
  if (status !== "running" || !lastStartedAt) return "—"
  const started = new Date(lastStartedAt).getTime()
  if (Number.isNaN(started)) return "—"
  const secs = Math.max(0, Math.floor((Date.now() - started) / 1000))
  const d = Math.floor(secs / 86400)
  const h = Math.floor((secs % 86400) / 3600)
  const min = Math.floor((secs % 3600) / 60)
  if (d > 0) return `${d}d ${h}h`
  if (h > 0) return `${h}h ${min}min`
  return `${min}min`
}

function Row({
  label,
  title,
  children,
}: {
  label: string
  title?: string
  children: React.ReactNode
}) {
  return (
    <>
      <dt className="text-muted-foreground" title={title}>
        {label}
      </dt>
      <dd className="min-w-0 break-words text-right">{children}</dd>
    </>
  )
}

export function TemplateInfoDialog({
  template,
  machines,
  open,
  onOpenChange,
}: {
  template: Template
  machines: Machine[]
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const envKeys = Object.keys(template.env ?? {})

  const [expandedId, setExpandedId] = React.useState<string | null>(null)
  const [detailsById, setDetailsById] = React.useState<
    Record<string, DetailsState>
  >({})

  function handleMachineOpenChange(id: string, next: boolean) {
    setExpandedId(next ? id : null)
    if (next && !detailsById[id]) {
      setDetailsById((s) => ({ ...s, [id]: { loading: true } }))
      getMachineInfoDetails(id)
        .then((data) =>
          setDetailsById((s) => ({ ...s, [id]: { loading: false, data } }))
        )
        .catch((e) =>
          setDetailsById((s) => ({
            ...s,
            [id]: {
              loading: false,
              error: e instanceof Error ? e.message : "Erro ao carregar",
            },
          }))
        )
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
        <DialogTitle className="sr-only">
          Informações do produto {template.name}
        </DialogTitle>

        <div className="text-muted-foreground -m-4 mb-0 flex items-center gap-2 border-b px-4 py-3 [&_svg]:size-4">
          <Info aria-hidden="true" className="shrink-0" />
          <span className="text-foreground min-w-0 truncate text-sm font-medium">
            {template.name}
          </span>
          <Badge variant="outline" className="ml-auto mr-8 shrink-0">
            {template.plan}
          </Badge>
        </div>

        <div className="space-y-4">
          {/* Produto */}
          <section className="space-y-2">
            <h3 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
              Produto
            </h3>
            <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2 text-sm">
              <Row label="Modelo">
                <span className="font-mono text-xs">{template.model_name}</span>
              </Row>
              <Row label="Imagem">
                <span className="font-mono text-xs">{template.image}</span>
              </Row>
              <Row label="GPUs">
                {template.gpu_types.length > 0
                  ? `${template.gpu_types.join(", ")}${
                      template.gpu_count > 1 ? ` ×${template.gpu_count}` : ""
                    }`
                  : "—"}
              </Row>
              <Row label="Disco">{template.disk_gb} GB</Row>
              <Row label="Volume">
                {template.volume_gb > 0
                  ? `${template.volume_gb} GB → ${template.volume_mount_path}`
                  : "—"}
              </Row>
              <Row label="Portas HTTP">
                {template.http_ports.length > 0
                  ? template.http_ports.join(", ")
                  : "—"}
              </Row>
              <Row label="Portas TCP">
                {template.tcp_ports.length > 0
                  ? template.tcp_ports.join(", ")
                  : "—"}
              </Row>
              <Row label="Footprint do modelo">
                {template.model_footprint_gb} GB
              </Row>
              <Row label="KV por usuário">
                {template.kv_reserve_gb_per_user} GB
              </Row>
              {template.lora_footprint_gb > 0 && (
                <Row label="Footprint LoRA">
                  {template.lora_footprint_gb} GB
                </Row>
              )}
              <Row label="Máx. usuários">{template.max_users ?? "—"}</Row>
              <Row label="Origem">
                {template.runpod_template_id ? (
                  <Badge variant="secondary">sincronizado</Badge>
                ) : (
                  <Badge variant="outline">só local</Badge>
                )}
              </Row>
              <Row label="Criado em">
                {new Date(template.created_at).toLocaleString("pt-BR")}
              </Row>
            </dl>

            {envKeys.length > 0 && (
              <div className="space-y-1 pt-1">
                <p className="text-muted-foreground text-xs">
                  Env vars ({envKeys.length}) — valores ocultos
                </p>
                <div className="flex flex-wrap gap-1">
                  {envKeys.map((k) => (
                    <Badge key={k} variant="outline" className="font-mono text-xs">
                      {k}
                    </Badge>
                  ))}
                </div>
              </div>
            )}

            {template.start_command && (
              <div className="space-y-1 pt-1">
                <p className="text-muted-foreground text-xs">Start command</p>
                <pre className="bg-muted overflow-x-auto rounded-md p-2 font-mono text-xs">
                  {template.start_command}
                </pre>
              </div>
            )}
          </section>

          <Separator />

          {/* Máquinas — colapsável, fechado por padrão */}
          <Collapsible className="space-y-2">
            <CollapsibleTrigger className="group flex w-full items-center gap-2">
              <h3 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
                Máquinas usando este produto ({machines.length})
              </h3>
              <ChevronDown
                aria-hidden="true"
                className="text-muted-foreground ml-auto size-4 shrink-0 transition-transform group-data-[state=open]:rotate-180"
              />
            </CollapsibleTrigger>
            <CollapsibleContent className="overflow-hidden data-[state=open]:animate-collapsible-down data-[state=closed]:animate-collapsible-up">
            <div className="space-y-2">
            {machines.length === 0 ? (
              <p className="text-muted-foreground text-sm">
                Nenhuma máquina usa este produto ainda.
              </p>
            ) : (
              <ul className="divide-y rounded-lg border">
                {machines.map((m) => {
                  const open = expandedId === m.id
                  const state = detailsById[m.id]
                  return (
                    <li key={m.id}>
                      <Collapsible
                        open={open}
                        onOpenChange={(next) =>
                          handleMachineOpenChange(m.id, next)
                        }
                      >
                        <CollapsibleTrigger asChild>
                          <button
                            type="button"
                            className="hover:bg-accent/50 flex w-full items-center gap-2 px-3 py-2 text-left text-sm"
                          >
                            <Server
                              aria-hidden="true"
                              className="text-muted-foreground size-4 shrink-0"
                            />
                            <span className="min-w-0 truncate">{m.name}</span>
                            <span className="ml-auto shrink-0">
                              <StatusBadge status={m.status} />
                            </span>
                            <ChevronDown
                              aria-hidden="true"
                              className={cn(
                                "text-muted-foreground size-4 shrink-0 transition-transform",
                                open && "rotate-180"
                              )}
                            />
                          </button>
                        </CollapsibleTrigger>
                        <CollapsibleContent className="overflow-hidden data-[state=open]:animate-collapsible-down data-[state=closed]:animate-collapsible-up">
                          <div className="px-3 pb-3">
                            {!state || state.loading ? (
                              <div className="text-muted-foreground flex items-center gap-2 py-1 text-xs">
                                <Spinner className="size-3.5" />
                                Carregando…
                              </div>
                            ) : state.error ? (
                              <p className="text-destructive py-1 text-xs">
                                {state.error}
                              </p>
                            ) : (
                              <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-1.5 text-sm">
                                <Row
                                  label="Pessoas agora"
                                  title="Chaves com requisições nos últimos 5 minutos"
                                >
                                  {state.data!.people}
                                </Row>
                                <Row label="Stacks">{state.data!.stacks}</Row>
                                <Row label="Tokens gerados">
                                  {(
                                    state.data!.tokensIn + state.data!.tokensOut
                                  ).toLocaleString("pt-BR")}
                                </Row>
                                <Row label="Requests">
                                  {state.data!.requests.toLocaleString("pt-BR")}
                                </Row>
                                <Row label="Tempo ligado">
                                  {formatUptime(
                                    m.status,
                                    state.data!.lastStartedAt
                                  )}
                                </Row>
                              </dl>
                            )}
                          </div>
                        </CollapsibleContent>
                      </Collapsible>
                    </li>
                  )
                })}
              </ul>
            )}
            </div>
            </CollapsibleContent>
          </Collapsible>
        </div>
      </DialogContent>
    </Dialog>
  )
}
