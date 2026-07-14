"use client"

import * as React from "react"
import { Check, Copy, TriangleAlert } from "lucide-react"
import { toast } from "sonner"

import { migrateStack } from "@/lib/actions"
import { computeCapacity } from "@/lib/capacity"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import type { StackInfo } from "@/components/contas/contas-table"
import type {
  StackMachine,
  StackTemplate,
} from "@/components/contas/create-stack-dialog"

// Sentinela do Select para "provisionar máquina nova" (o Select do Radix
// não aceita item com value vazio).
const NEW_MACHINE = "__new__"

type Result = { machineCreated: boolean; plainKey: string | null }

// O chamador deve remontar via `key` a cada abertura para resetar o
// estado interno (alvo, resultado, copiado) — padrão dos dialogs de conta.
export function MigrateStackDialog({
  stack,
  machines,
  templates,
  open,
  onOpenChange,
}: {
  stack: StackInfo
  machines: StackMachine[]
  templates: StackTemplate[]
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  // Mesmo template de referência da action: o da máquina atual; sem
  // máquina, o produto cadastrado com o plano da stack.
  const templateId =
    stack.machine?.template_id ??
    templates.find((t) => t.plan === stack.plan)?.id
  const template = templates.find((t) => t.id === templateId)

  // Máquinas elegíveis: rodando, do mesmo produto, com vaga livre e
  // diferente da atual — mesma semântica do create-stack-dialog.
  const eligible = React.useMemo(() => {
    if (!template) return []
    return machines.filter((m) => {
      if (m.id === stack.machine_id) return false
      if (m.template_id !== template.id) return false
      const cap = computeCapacity({
        vramGb: m.vram_gb,
        modelFootprintGb: template.model_footprint_gb ?? 16,
        kvReserveGbPerUser: template.kv_reserve_gb_per_user ?? 2,
        activeKeys: m.activeKeys,
        maxUsers: m.max_users,
      })
      return !(cap.slotsMax > 0 && cap.slotsUsed >= cap.slotsMax)
    })
  }, [machines, template, stack.machine_id])

  const [target, setTarget] = React.useState<string>(
    () => eligible[0]?.id ?? NEW_MACHINE
  )
  const [result, setResult] = React.useState<Result | null>(null)
  const [copied, setCopied] = React.useState(false)
  const [pending, startTransition] = React.useTransition()

  function slotsLabel(m: StackMachine) {
    if (!template) return ""
    const cap = computeCapacity({
      vramGb: m.vram_gb,
      modelFootprintGb: template.model_footprint_gb ?? 16,
      kvReserveGbPerUser: template.kv_reserve_gb_per_user ?? 2,
      activeKeys: m.activeKeys,
      maxUsers: m.max_users,
    })
    return cap.slotsMax > 0 ? ` (${cap.slotsFree} vaga(s))` : ""
  }

  function onConfirm() {
    startTransition(async () => {
      try {
        const res = await migrateStack({
          stackId: stack.id,
          targetMachineId: target === NEW_MACHINE ? null : target,
        })
        setResult(res)
        toast.success(`Stack ${stack.slug} migrada`)
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "Erro ao migrar stack")
      }
    })
  }

  async function copyKey() {
    if (!result?.plainKey) return
    await navigator.clipboard.writeText(result.plainKey)
    setCopied(true)
    toast.success("Chave copiada")
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Migrar stack</DialogTitle>
            <DialogDescription>
              Move a stack{" "}
              <code className="font-mono text-xs">{stack.slug}</code>
              {stack.machineName ? ` da máquina ${stack.machineName}` : ""} para
              outra máquina do mesmo produto.
            </DialogDescription>
          </DialogHeader>

          {result ? (
            <div className="flex flex-col gap-4">
              {result.plainKey ? (
                <>
                  <Alert>
                    <TriangleAlert />
                    <AlertTitle>Guarde esta chave agora</AlertTitle>
                    <AlertDescription>
                      Nova chave da conta na máquina de destino. Ela não será
                      exibida novamente — armazenamos apenas o hash.
                    </AlertDescription>
                  </Alert>
                  <div className="flex items-center gap-2">
                    <code className="flex-1 break-all rounded-lg border bg-muted p-3 font-mono text-xs">
                      {result.plainKey}
                    </code>
                    <Button variant="outline" size="icon" onClick={copyKey}>
                      {copied ? (
                        <Check className="size-4" />
                      ) : (
                        <Copy className="size-4" />
                      )}
                    </Button>
                  </div>
                </>
              ) : (
                <p className="text-sm text-muted-foreground">
                  A conta já tinha uma chave ativa na máquina de destino — ela
                  foi reutilizada, nenhuma chave nova foi emitida.
                </p>
              )}
              <p className="text-xs text-muted-foreground">
                {result.machineCreated &&
                  "A máquina está subindo — a chave será sincronizada quando o pod ficar pronto."}
              </p>
              <Button onClick={() => onOpenChange(false)}>Concluir</Button>
            </div>
          ) : (
            <div className="flex flex-col gap-4">
              <div className="flex flex-col gap-2">
                <Label>Máquina de destino</Label>
                <Select value={target} onValueChange={setTarget}>
                  <SelectTrigger className="w-full">
                    <SelectValue placeholder="Escolha a máquina" />
                  </SelectTrigger>
                  <SelectContent>
                    {eligible.map((m) => (
                      <SelectItem key={m.id} value={m.id}>
                        {m.name} — {m.model_name}
                        {slotsLabel(m)}
                      </SelectItem>
                    ))}
                    <SelectItem value={NEW_MACHINE}>
                      Provisionar nova máquina (llm-stack-N)
                    </SelectItem>
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  Máquinas rodando com o mesmo produto e vaga livre. As chaves
                  da conta na máquina antiga são revogadas se nenhuma outra
                  stack dela continuar lá.
                </p>
              </div>
              <Button onClick={onConfirm} disabled={pending || !target}>
                {pending
                  ? target === NEW_MACHINE
                    ? "Criando máquina… (~1 min)"
                    : "Migrando…"
                  : "Migrar"}
              </Button>
            </div>
          )}
      </DialogContent>
    </Dialog>
  )
}
