"use client"

import * as React from "react"
import { Plus } from "lucide-react"
import { toast } from "sonner"

import { createTemplate } from "@/lib/actions"
import { Button } from "@/components/ui/button"
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
import { Textarea } from "@/components/ui/textarea"

export function CreateTemplateDialog() {
  const [open, setOpen] = React.useState(false)
  const [pending, startTransition] = React.useTransition()

  function onSubmit(formData: FormData) {
    startTransition(async () => {
      try {
        await createTemplate(formData)
        toast.success("Template criado")
        setOpen(false)
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "Erro ao criar template")
      }
    })
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button>
          <Plus /> Novo template
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Novo template</DialogTitle>
          <DialogDescription>
            Define a imagem Docker, o modelo e os parâmetros de capacidade.
          </DialogDescription>
        </DialogHeader>
        <form action={onSubmit} className="flex flex-col gap-4">
          <div className="grid grid-cols-2 gap-4">
            <div className="flex flex-col gap-2">
              <Label htmlFor="name">Nome</Label>
              <Input id="name" name="name" placeholder="vllm-qwen-7b" required />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="model_name">Modelo</Label>
              <Input
                id="model_name"
                name="model_name"
                placeholder="Qwen/Qwen2.5-7B-Instruct"
                required
              />
            </div>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="image">Imagem Docker</Label>
            <Input
              id="image"
              name="image"
              placeholder="seuusuario/vllm-agent:latest"
              required
            />
          </div>
          <div className="grid grid-cols-3 gap-4">
            <div className="flex flex-col gap-2">
              <Label htmlFor="disk_gb">Disco (GB)</Label>
              <Input id="disk_gb" name="disk_gb" type="number" defaultValue={40} />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="model_footprint_gb">Modelo (GB VRAM)</Label>
              <Input
                id="model_footprint_gb"
                name="model_footprint_gb"
                type="number"
                step="0.5"
                defaultValue={16}
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="kv_reserve_gb_per_user">Reserva/usuário (GB)</Label>
              <Input
                id="kv_reserve_gb_per_user"
                name="kv_reserve_gb_per_user"
                type="number"
                step="0.5"
                defaultValue={2}
              />
            </div>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="gpu_types">GPUs compatíveis (IDs, separados por vírgula)</Label>
            <Input
              id="gpu_types"
              name="gpu_types"
              placeholder="NVIDIA GeForce RTX 4090, NVIDIA RTX A5000"
            />
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="env">Variáveis de ambiente (JSON)</Label>
            <Textarea
              id="env"
              name="env"
              rows={3}
              defaultValue={`{}`}
              className="font-mono text-xs"
            />
          </div>
          <Button type="submit" disabled={pending}>
            {pending ? "Criando…" : "Criar template"}
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  )
}
