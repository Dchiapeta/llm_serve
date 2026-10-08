"use client"

import * as React from "react"
import { Plus } from "lucide-react"
import { toast } from "sonner"

import { createOpenRouterModel } from "@/lib/actions"
import { TEMPLATE_PLANS } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

export function CreateOpenRouterModelDialog() {
  const [open, setOpen] = React.useState(false)
  const [pending, startTransition] = React.useTransition()

  function onSubmit(formData: FormData) {
    startTransition(async () => {
      const result = await createOpenRouterModel(formData)
      if (result?.error) {
        toast.error(result.error)
        return
      }
      toast.success("Modelo adicionado")
      setOpen(false)
    })
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button>
          <Plus /> Novo modelo
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Novo modelo</DialogTitle>
          <DialogDescription>
            Use o ID exato do modelo no OpenRouter (o que aparece em
            openrouter.ai/models). É esse ID que o cliente manda no campo
            &quot;model&quot;. Se é de texto, de imagem ou de decisão (Jev), o painel
            descobre sozinho pelo catálogo.
          </DialogDescription>
        </DialogHeader>
        <form action={onSubmit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor="slug">ID no OpenRouter</Label>
            <Input
              id="slug"
              name="slug"
              placeholder="anthropic/claude-sonnet-4.5"
              className="font-mono"
              autoComplete="off"
              required
            />
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="label">Nome (opcional)</Label>
            <Input id="label" name="label" placeholder="O nome do catálogo, se vazio" />
          </div>
          <fieldset className="flex flex-col gap-2">
            <legend className="mb-2 text-sm font-medium">Planos com acesso</legend>
            <div className="flex flex-wrap gap-4">
              {TEMPLATE_PLANS.map((plan) => (
                <div key={plan} className="flex items-center gap-2">
                  <Checkbox id={`plan-${plan}`} name="plans" value={plan} defaultChecked />
                  <Label htmlFor={`plan-${plan}`} className="font-normal">
                    {plan}
                  </Label>
                </div>
              ))}
            </div>
          </fieldset>
          <Button type="submit" disabled={pending}>
            {pending ? "Adicionando…" : "Adicionar"}
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  )
}
