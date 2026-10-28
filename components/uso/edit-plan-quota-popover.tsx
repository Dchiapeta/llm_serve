"use client"

import * as React from "react"
import { Pencil } from "lucide-react"
import { toast } from "sonner"

import { setPlanRequestQuota } from "@/lib/actions"
import type { PlanRequestQuota } from "@/lib/plan-request-quotas"
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

const fmt = (n: number) => n.toLocaleString("pt-BR")

// Troca o limite padrão de um plano (migration 0080). Antes de salvar mostra
// quantas stacks mudam de fato — as que têm override ficam no número delas.
export function EditPlanQuotaPopover({
  quota,
  affectedStacks,
}: {
  quota: PlanRequestQuota
  /** Stacks do plano sem request_quota_override. */
  affectedStacks: number
}) {
  const [open, setOpen] = React.useState(false)
  const [value, setValue] = React.useState(String(quota.limit))
  const [pending, startTransition] = React.useTransition()
  const next = Number(value)
  const valid = Number.isInteger(next) && next > 0
  const changed = valid && next !== quota.limit

  function onOpenChange(nextOpen: boolean) {
    // Reabrir sempre parte do limite atual, não do que ficou digitado.
    if (nextOpen) setValue(String(quota.limit))
    setOpen(nextOpen)
  }

  function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!valid) {
      toast.error("Informe um número inteiro maior que zero")
      return
    }
    if (!changed) {
      setOpen(false)
      return
    }
    startTransition(async () => {
      const result = await setPlanRequestQuota(quota.plan, next)
      if (result?.error) {
        toast.error(result.error)
        return
      }
      toast.success(`Limite do ${quota.plan} alterado para ${fmt(next)}`)
      setOpen(false)
    })
  }

  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="size-7"
          aria-label={`Editar limite do plano ${quota.plan}`}
        >
          <Pencil className="size-3.5" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-72">
        <form onSubmit={onSubmit} className="space-y-3">
          <PopoverHeader>
            <PopoverTitle>Limite do {quota.plan}</PopoverTitle>
            <PopoverDescription>
              Vale para {fmt(affectedStacks)}{" "}
              {affectedStacks === 1 ? "stack" : "stacks"} sem limite próprio, no
              ciclo atual. O gateway aplica em até 1 minuto e o site atualiza
              os textos dos planos em até 5.
            </PopoverDescription>
          </PopoverHeader>
          <div className="space-y-1.5">
            <Label htmlFor={`plan-quota-${quota.plan}`}>Requisições por mês</Label>
            <Input
              id={`plan-quota-${quota.plan}`}
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
          {changed && next < quota.limit && (
            <p className="text-xs text-muted-foreground">
              Reduzir pode cortar na hora quem já passou de {fmt(next)} neste
              ciclo.
            </p>
          )}
          <div className="flex justify-end">
            <Button type="submit" size="sm" disabled={pending || !changed}>
              Salvar
            </Button>
          </div>
        </form>
      </PopoverContent>
    </Popover>
  )
}
