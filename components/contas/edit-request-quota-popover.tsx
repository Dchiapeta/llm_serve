"use client"

import * as React from "react"
import { Pencil } from "lucide-react"
import { toast } from "sonner"

import { setStackRequestQuota } from "@/lib/actions"
import { quotaIsCustom, type RequestQuota } from "@/lib/request-quota"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Popover,
  PopoverContent,
  PopoverDescription,
  PopoverHeader,
  PopoverTitle,
  PopoverTrigger,
} from "@/components/ui/popover"

// Troca o limite mensal de requisições de uma stack (migration 0077). Só
// aparece quando a função de cota informa o padrão do plano — sem isso a
// coluna de override ainda não existe e salvar falharia.
export function EditRequestQuotaPopover({
  stackId,
  stackName,
  quota,
}: {
  stackId: string
  stackName: string
  quota: RequestQuota & { defaultLimit: number }
}) {
  const [open, setOpen] = React.useState(false)
  const [value, setValue] = React.useState(String(quota.limit))
  const [pending, startTransition] = React.useTransition()
  const custom = quotaIsCustom(quota)

  function onOpenChange(next: boolean) {
    // Reabrir sempre parte do limite atual, não do que ficou digitado.
    if (next) setValue(String(quota.limit))
    setOpen(next)
  }

  function save(limit: number | null) {
    startTransition(async () => {
      const result = await setStackRequestQuota(stackId, limit)
      if (result?.error) {
        toast.error(result.error)
        return
      }
      toast.success(
        limit === null
          ? `Limite de ${stackName} voltou ao padrão do plano`
          : `Limite de ${stackName} alterado para ${limit.toLocaleString("pt-BR")}`
      )
      setOpen(false)
    })
  }

  function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    const limit = Number(value)
    if (!Number.isInteger(limit) || limit <= 0) {
      toast.error("Informe um número inteiro maior que zero")
      return
    }
    // Digitar o próprio padrão é o mesmo que restaurar: guardar o número
    // prenderia a stack nele se o padrão do plano mudar depois.
    save(limit === quota.defaultLimit ? null : limit)
  }

  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="size-6"
          aria-label={`Editar limite de requisições de ${stackName}`}
        >
          <Pencil className="size-3" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-64">
        <form onSubmit={onSubmit} className="space-y-3">
          <PopoverHeader>
            <PopoverTitle>Limite mensal</PopoverTitle>
            <PopoverDescription>
              Padrão do plano: {quota.defaultLimit.toLocaleString("pt-BR")}{" "}
              requisições. Vale em até 1 minuto no gateway.
            </PopoverDescription>
          </PopoverHeader>
          <div className="space-y-1.5">
            <Label htmlFor={`quota-${stackId}`}>Requisições por ciclo</Label>
            <Input
              id={`quota-${stackId}`}
              type="number"
              inputMode="numeric"
              min={1}
              step={1}
              value={value}
              onChange={(e) => setValue(e.target.value)}
              disabled={pending}
              autoFocus
            />
          </div>
          <div className="flex justify-end gap-2">
            {custom && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={pending}
                onClick={() => save(null)}
              >
                Restaurar padrão
              </Button>
            )}
            <Button type="submit" size="sm" disabled={pending}>
              Salvar
            </Button>
          </div>
        </form>
      </PopoverContent>
    </Popover>
  )
}
