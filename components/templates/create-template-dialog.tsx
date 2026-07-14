"use client"

import * as React from "react"
import { Plus } from "lucide-react"
import { toast } from "sonner"

import { createTemplate } from "@/lib/actions"
import type { GpuType } from "@/lib/runpod"
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
import { Textarea } from "@/components/ui/textarea"
import { CollapsibleSection } from "@/components/templates/collapsible-section"
import { TEMPLATE_PLANS } from "@/lib/types"

export function CreateTemplateDialog({ gpus }: { gpus: GpuType[] }) {
  const [open, setOpen] = React.useState(false)
  const [pending, startTransition] = React.useTransition()

  function onSubmit(formData: FormData) {
    startTransition(async () => {
      try {
        await createTemplate(formData)
        toast.success("Produto criado")
        setOpen(false)
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "Erro ao criar produto")
      }
    })
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button>
          <Plus /> Novo produto
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[90vh] grid-rows-[auto_1fr] overflow-hidden sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Novo produto</DialogTitle>
          <DialogDescription>
            Define a imagem Docker, o modelo e os parâmetros de capacidade.
          </DialogDescription>
        </DialogHeader>
        <form action={onSubmit} className="flex min-h-0 flex-col gap-4 overflow-y-auto pr-1">
          <div className="flex flex-col gap-2">
            <Label htmlFor="name">Nome</Label>
            <Input id="name" name="name" placeholder="vllm-qwen-7b" required />
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="plan">Plano</Label>
            <Select name="plan" defaultValue="VibeCoder">
              <SelectTrigger id="plan" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {TEMPLATE_PLANS.map((p) => (
                  <SelectItem key={p} value={p}>
                    {p}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="model_name">Modelo (Hugging Face)</Label>
            <Input
              id="model_name"
              name="model_name"
              placeholder="Qwen/Qwen2.5-7B-Instruct"
              pattern="[^/\s]+/[^/\s]+"
              title="Use o ID do repositório do Hugging Face no formato org/modelo"
              required
            />
            <p className="text-xs text-muted-foreground">
              ID do repositório no Hugging Face (org/modelo). Copie de
              huggingface.co e cole aqui — ex.: Qwen/Qwen2.5-7B-Instruct
            </p>
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
          <div className="flex flex-col gap-2">
            <Label htmlFor="start_command">Container start command</Label>
            <Textarea
              id="start_command"
              name="start_command"
              rows={6}
              placeholder={"--model org/modelo\n--served-model-name meu-modelo\n--port 8000"}
              className="font-mono text-xs"
            />
            <p className="text-xs text-muted-foreground">
              Argumentos passados ao container na inicialização. Um por linha ou
              separados por espaço.
            </p>
          </div>
          <div className="grid grid-cols-2 gap-4">
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
            <div className="flex flex-col gap-2">
              <Label htmlFor="lora_footprint_gb">Adapter LoRA (GB)</Label>
              <Input
                id="lora_footprint_gb"
                name="lora_footprint_gb"
                type="number"
                step="0.1"
                defaultValue={0.5}
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="max_users">Máx. usuários</Label>
              <Input
                id="max_users"
                name="max_users"
                type="number"
                min={1}
                step={1}
                placeholder="automático (VRAM)"
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="gpu_count">Quantidade de GPUs</Label>
              <Input
                id="gpu_count"
                name="gpu_count"
                type="number"
                min={1}
                step={1}
                defaultValue={1}
              />
            </div>
          </div>
          <p className="text-xs text-muted-foreground">
            Máx. usuários limita quantas chaves ativas a máquina aceita. Vazio =
            calculado pela VRAM da GPU. Adapter LoRA (GB) é o custo de VRAM por
            adapter carregado (depende do rank; meça com scripts/test-lora-load.mjs
            + nvidia-smi).
          </p>
          <div className="flex flex-col gap-2">
            <Label>GPUs compatíveis</Label>
            {gpus.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                Nenhuma GPU disponível (falha ao consultar o RunPod).
              </p>
            ) : (
              <div className="max-h-48 overflow-y-auto rounded-md border p-2">
                {gpus.map((g) => (
                  <label
                    key={g.id}
                    className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-sm hover:bg-muted"
                  >
                    <input
                      type="checkbox"
                      name="gpu_types"
                      value={g.id}
                      className="size-4 accent-primary"
                    />
                    <span className="flex-1">{g.displayName}</span>
                    <span className="text-xs text-muted-foreground">
                      {g.memoryInGb} GB
                      {g.securePrice ? ` · $${g.securePrice.toFixed(2)}/h` : ""}
                    </span>
                  </label>
                ))}
              </div>
            )}
            <p className="text-xs text-muted-foreground">
              Selecione uma ou mais GPUs em que este modelo pode rodar.
            </p>
          </div>
          <CollapsibleSection title="Storage configuration">
            <div className="flex flex-col gap-2">
              <Label htmlFor="disk_gb">Container disk (GB)</Label>
              <Input
                id="disk_gb"
                name="disk_gb"
                type="number"
                min={0}
                defaultValue={40}
              />
              <p className="text-xs text-muted-foreground">
                Armazenamento temporário, apagado quando o pod é parado.
              </p>
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="volume_gb">Persistent storage (GB)</Label>
              <Input
                id="volume_gb"
                name="volume_gb"
                type="number"
                min={0}
                defaultValue={0}
              />
              <p className="text-xs text-muted-foreground">
                Volume persistente montado no pod. 0 = sem volume.
              </p>
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="volume_mount_path">
                Persistent storage mount path
              </Label>
              <Input
                id="volume_mount_path"
                name="volume_mount_path"
                placeholder="/workspace"
                defaultValue="/workspace"
              />
            </div>
          </CollapsibleSection>

          <CollapsibleSection title="Networking configuration">
            <div className="flex flex-col gap-2">
              <Label htmlFor="http_ports">HTTP Ports</Label>
              <Input
                id="http_ports"
                name="http_ports"
                placeholder="8000"
                defaultValue="8000"
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="tcp_ports">TCP Ports</Label>
              <Input id="tcp_ports" name="tcp_ports" placeholder="22" />
            </div>
            <p className="text-xs text-muted-foreground">
              Números de porta separados por vírgula. Ex.: 8000, 8080
            </p>
          </CollapsibleSection>

          <CollapsibleSection title="Environment variables">
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
          </CollapsibleSection>

          <Button type="submit" disabled={pending}>
            {pending ? "Criando…" : "Criar produto"}
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  )
}
