"use client"

import * as React from "react"
import { Layers } from "lucide-react"
import { toast } from "sonner"

import { registerLoraAdapter } from "@/lib/actions"
import type { Stack } from "@/lib/types"
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

type StackOption = Stack & { accounts: { name: string } | null }

export function RegisterLoraDialog({ stacks }: { stacks: StackOption[] }) {
  const [open, setOpen] = React.useState(false)
  const [stackId, setStackId] = React.useState<string>("")
  const [version, setVersion] = React.useState<string>("")
  const [pending, startTransition] = React.useTransition()

  function onRegister() {
    startTransition(async () => {
      try {
        const formData = new FormData()
        formData.set("stack_id", stackId)
        formData.set("version", version)
        await registerLoraAdapter(formData)
        toast.success("Adapter registrado")
        setOpen(false)
        setStackId("")
        setVersion("")
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "Erro ao registrar adapter")
      }
    })
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline">
          <Layers /> Registrar LoRA
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Registrar adapter LoRA</DialogTitle>
          <DialogDescription>
            Registra um adapter já enviado ao bucket <code>loras</code> do
            Supabase Storage, no prefixo{" "}
            <code>{"{conta}/{versão}/"}</code> com{" "}
            <code>adapter_config.json</code> e{" "}
            <code>adapter_model.safetensors</code>.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label>Stack</Label>
            <Select value={stackId} onValueChange={setStackId}>
              <SelectTrigger>
                <SelectValue placeholder="Escolha a stack" />
              </SelectTrigger>
              <SelectContent>
                {stacks.map((s) => (
                  <SelectItem key={s.id} value={s.id}>
                    {s.accounts?.name ?? "?"} — {s.slug} ({s.plan})
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="lora-version">Versão</Label>
            <Input
              id="lora-version"
              placeholder="ex: v1"
              value={version}
              onChange={(e) => setVersion(e.target.value)}
            />
          </div>
          <Button
            onClick={onRegister}
            disabled={pending || !stackId || !version.trim()}
          >
            {pending ? "Registrando…" : "Registrar adapter"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
