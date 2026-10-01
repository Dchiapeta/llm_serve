"use client"

import * as React from "react"
import { ChevronDown, LifeBuoy, MoreHorizontal, Trash2 } from "lucide-react"
import { toast } from "sonner"

import {
  deleteOpenRouterModel,
  setOpenRouterFallback,
  setOpenRouterModelEnabled,
  setOpenRouterModelPlans,
} from "@/lib/actions"
import { PLAN_BADGE_VARIANT } from "@/lib/plan-badge"
import { TEMPLATE_PLANS, type OpenRouterModel, type TemplatePlan } from "@/lib/types"
import { Badge } from "@/components/reui/badge"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Switch } from "@/components/ui/switch"

// Liga/desliga um modelo da allowlist sem apagá-lo: desligado, o gateway para
// de aceitá-lo em até alguns segundos (o painel pede o flush do cache).
export function OpenRouterModelEnabledSwitch({ model }: { model: OpenRouterModel }) {
  const [enabled, setEnabled] = React.useState(model.enabled)
  const [pending, startTransition] = React.useTransition()

  return (
    <Switch
      size="sm"
      checked={enabled}
      disabled={pending}
      aria-label={`Ativar ${model.slug}`}
      onCheckedChange={(next) =>
        startTransition(async () => {
          const result = await setOpenRouterModelEnabled(model.id, next)
          if (result?.error) {
            toast.error(result.error)
            return
          }
          setEnabled(next)
        })
      }
    />
  )
}

// Planos com acesso ao modelo (migration 0074). Fora deles o gateway responde
// 403 — é o que faz o Go não alcançar o Qwen 3.8 27B. Sem plano nenhum o
// modelo fica bloqueado para todos (Kimi K3, ainda não lançado). Cada clique
// salva na hora, como o switch de Ativo.
export function OpenRouterModelPlansSelect({ model }: { model: OpenRouterModel }) {
  const [plans, setPlans] = React.useState<TemplatePlan[]>(model.plans ?? [])
  const [pending, startTransition] = React.useTransition()

  function toggle(plan: TemplatePlan, checked: boolean) {
    const next = TEMPLATE_PLANS.filter((p) => (p === plan ? checked : plans.includes(p)))
    startTransition(async () => {
      const result = await setOpenRouterModelPlans(model.id, next)
      if (result?.error) {
        toast.error(result.error)
        return
      }
      setPlans(next)
    })
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          disabled={pending}
          aria-label={`Planos de ${model.slug}`}
          className="h-auto min-h-8 gap-1 px-2"
        >
          {plans.length === 0 ? (
            <span className="text-destructive">Bloqueado (nenhum plano)</span>
          ) : (
            <span className="flex flex-wrap gap-1">
              {plans.map((plan) => (
                <Badge key={plan} variant={PLAN_BADGE_VARIANT[plan]} size="sm">
                  {plan}
                </Badge>
              ))}
            </span>
          )}
          <ChevronDown className="size-3.5 text-muted-foreground" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start">
        <DropdownMenuLabel>Planos com acesso</DropdownMenuLabel>
        {TEMPLATE_PLANS.map((plan) => (
          <DropdownMenuCheckboxItem
            key={plan}
            checked={plans.includes(plan)}
            disabled={pending}
            onSelect={(e) => e.preventDefault()}
            onCheckedChange={(checked) => toggle(plan, checked === true)}
          >
            {plan}
          </DropdownMenuCheckboxItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export function OpenRouterModelRowActions({ model }: { model: OpenRouterModel }) {
  const [deleteOpen, setDeleteOpen] = React.useState(false)
  const [pending, startTransition] = React.useTransition()
  const fallbackPlans = model.fallback_plans ?? []
  // reserva só entre os planos que já alcançam o modelo (check da 0074)
  const fallbackCandidates =
    model.kind === "text"
      ? (model.plans ?? []).filter((plan) => !fallbackPlans.includes(plan))
      : []

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" size="icon" aria-label={`Ações para ${model.slug}`}>
            <MoreHorizontal className="size-4" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          {fallbackCandidates.length > 0 && (
            <>
              {fallbackCandidates.map((plan) => (
                <DropdownMenuItem
                  key={plan}
                  disabled={pending}
                  onSelect={() =>
                    startTransition(async () => {
                      const result = await setOpenRouterFallback(model.id, plan)
                      if (result?.error) {
                        toast.error(result.error)
                        return
                      }
                      toast.success(`${model.slug} é o novo reserva do ${plan}`)
                    })
                  }
                >
                  <LifeBuoy className="size-4" />
                  Usar como reserva do {plan}
                </DropdownMenuItem>
              ))}
              <DropdownMenuSeparator />
            </>
          )}
          <DropdownMenuItem variant="destructive" onSelect={() => setDeleteOpen(true)}>
            <Trash2 className="size-4" />
            Remover
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <AlertDialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Remover {model.slug}?</AlertDialogTitle>
            <AlertDialogDescription>
              Clientes que usam este modelo passam a receber erro. O histórico de
              requisições dele continua em Requisições.
              {fallbackPlans.length > 0 &&
                ` Ele é o reserva de texto do ${fallbackPlans.join(", ")}: sem reserva, requisições sem máquina disponível nesses planos voltam a receber erro.`}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancelar</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              disabled={pending}
              onClick={(e) => {
                e.preventDefault()
                startTransition(async () => {
                  const result = await deleteOpenRouterModel(model.id)
                  if (result?.error) {
                    toast.error(result.error)
                    return
                  }
                  toast.success("Modelo removido")
                  setDeleteOpen(false)
                })
              }}
            >
              Remover
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
