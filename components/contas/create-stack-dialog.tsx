"use client"

import * as React from "react"
import { Check, Copy, Plus, RefreshCw, TriangleAlert } from "lucide-react"
import { toast } from "sonner"

import { createStack } from "@/lib/actions"
import { computeCapacity } from "@/lib/capacity"
import { generateStackSlug } from "@/lib/slug"
import {
  TEMPLATE_PLANS,
  type Account,
  type Template,
  type TemplatePlan,
} from "@/lib/types"
import {
  Autocomplete,
  AutocompleteContent,
  AutocompleteEmpty,
  AutocompleteInput,
  AutocompleteItem,
  AutocompleteList,
} from "@/components/reui/autocomplete"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
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

export type StackTemplate = Pick<
  Template,
  "id" | "name" | "plan" | "model_name" | "model_footprint_gb" | "kv_reserve_gb_per_user" | "gpu_types"
>

export type StackMachine = {
  id: string
  name: string
  template_id: string
  model_name: string | null
  vram_gb: number | null
  max_users: number | null
  activeKeys: number
}

type Phase = "form" | "confirm" | "done"

type StackResult = {
  slug: string
  machineCreated: boolean
  plainKey: string
}

export function CreateStackDialog({
  accounts,
  templates,
  machines,
}: {
  accounts: Pick<Account, "id" | "name" | "email">[]
  templates: StackTemplate[]
  machines: StackMachine[]
}) {
  const [open, setOpen] = React.useState(false)
  const [phase, setPhase] = React.useState<Phase>("form")
  const [email, setEmail] = React.useState("")
  const [name, setName] = React.useState("")
  const [plan, setPlan] = React.useState<TemplatePlan>("VibeCoder")
  const [templateId, setTemplateId] = React.useState("")
  const [machineId, setMachineId] = React.useState("")
  const [slug, setSlug] = React.useState(generateStackSlug)
  const [result, setResult] = React.useState<StackResult | null>(null)
  const [copied, setCopied] = React.useState(false)
  const [pending, startTransition] = React.useTransition()
  const formRef = React.useRef<HTMLFormElement>(null)
  // O popup do autocomplete precisa ser portalizado para dentro do dialog:
  // fora dele o Radix modal bloqueia pointer-events e os cliques não chegam.
  const contentRef = React.useRef<HTMLDivElement>(null)

  const emailItems = React.useMemo(
    () =>
      accounts
        .filter((a) => a.email)
        .map((a) => ({
          value: a.email as string,
          label: `${a.name} — ${a.email}`,
        })),
    [accounts]
  )

  const existing = accounts.find(
    (a) => a.email?.toLowerCase() === email.trim().toLowerCase()
  )

  const template = templates.find((t) => t.id === templateId)

  // Máquinas elegíveis: rodando, do template escolhido, com slot livre —
  // mesma semântica de createKey (slotsMax 0 = desconhecida, não bloqueia).
  const eligible = React.useMemo(() => {
    if (!template) return []
    return machines.filter((m) => {
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
  }, [machines, template])

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

  function onOpenChange(next: boolean) {
    setOpen(next)
    if (next) {
      setPhase("form")
      setEmail("")
      setName("")
      setPlan("VibeCoder")
      setTemplateId("")
      setMachineId("")
      setSlug(generateStackSlug())
      setResult(null)
      setCopied(false)
    }
  }

  function onEmailChange(value: string) {
    setEmail(value)
    const match = accounts.find(
      (a) => a.email?.toLowerCase() === value.trim().toLowerCase()
    )
    if (match) setName(match.name)
  }

  function toConfirm() {
    if (!formRef.current?.reportValidity()) return
    if (!templateId) {
      toast.error("Selecione um template")
      return
    }
    setMachineId(eligible[0]?.id ?? "")
    setPhase("confirm")
  }

  function onSubmit(formData: FormData) {
    startTransition(async () => {
      try {
        const res = await createStack(formData)
        setResult(res)
        setPhase("done")
        toast.success(`Stack criada — ID: ${res.slug}`)
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "Erro ao criar stack")
      }
    })
  }

  async function copyKey() {
    if (!result) return
    await navigator.clipboard.writeText(result.plainKey)
    setCopied(true)
    toast.success("Chave copiada")
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogTrigger asChild>
        <Button>
          <Plus /> Nova stack
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md" ref={contentRef}>
        <DialogHeader>
          <DialogTitle>Nova stack</DialogTitle>
          <DialogDescription>
            Uma stack é uma LLM contratada por um cliente. Se o e-mail já
            tiver conta, a stack é adicionada a ela.
          </DialogDescription>
        </DialogHeader>

        {phase === "done" && result ? (
          <div className="flex flex-col gap-4">
            <Alert>
              <TriangleAlert />
              <AlertTitle>Guarde esta chave agora</AlertTitle>
              <AlertDescription>
                Ela não será exibida novamente — armazenamos apenas o hash.
              </AlertDescription>
            </Alert>
            <div className="flex items-center gap-2">
              <code className="flex-1 break-all rounded-lg border bg-muted p-3 font-mono text-xs">
                {result.plainKey}
              </code>
              <Button variant="outline" size="icon" onClick={copyKey}>
                {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              Stack <code className="font-mono">{result.slug}</code> criada.
              {result.machineCreated &&
                " A máquina está subindo — a chave será sincronizada quando o pod ficar pronto."}
            </p>
            <Button onClick={() => setOpen(false)}>Concluir</Button>
          </div>
        ) : (
          <form ref={formRef} action={onSubmit} className="flex flex-col gap-4">
            <div className={phase === "form" ? "flex flex-col gap-4" : "hidden"}>
              <div className="flex flex-col gap-2">
                <Label htmlFor="email">E-mail do cliente</Label>
                <Autocomplete
                  items={emailItems}
                  value={email}
                  onValueChange={onEmailChange}
                  // Ao clicar num item, preenche o input com o e-mail (sem isso,
                  // itens {value,label} usariam o label "Nome — e-mail").
                  itemToStringValue={(item) => item.value}
                  // Busca por nome ou e-mail (o filtro padrão usaria só o e-mail
                  // por causa do itemToStringValue acima).
                  filter={(item, query) =>
                    item.label.toLowerCase().includes(query.trim().toLowerCase())
                  }
                >
                  <AutocompleteInput
                    id="email"
                    type="email"
                    placeholder="cliente@exemplo.com"
                    required
                    showClear
                  />
                  <AutocompleteContent container={contentRef}>
                    <AutocompleteEmpty>Nenhuma conta com esse e-mail.</AutocompleteEmpty>
                    <AutocompleteList>
                      {(item) => (
                        <AutocompleteItem key={item.value} value={item}>
                          {item.label}
                        </AutocompleteItem>
                      )}
                    </AutocompleteList>
                  </AutocompleteContent>
                </Autocomplete>
                {existing && (
                  <p className="text-xs text-muted-foreground">
                    Stack será adicionada à conta existente{" "}
                    <span className="font-medium">{existing.name}</span>.
                  </p>
                )}
              </div>
              <div className="flex flex-col gap-2">
                <Label htmlFor="name">Nome do cliente</Label>
                <Input
                  id="name"
                  name="name"
                  required
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  readOnly={!!existing}
                />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div className="flex min-w-0 flex-col gap-2">
                  <Label>Produto</Label>
                  <Select value={plan} onValueChange={(v) => setPlan(v as TemplatePlan)}>
                    <SelectTrigger className="w-full">
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
                <div className="flex min-w-0 flex-col gap-2">
                  <Label>Template</Label>
                  <Select value={templateId} onValueChange={setTemplateId}>
                    <SelectTrigger className="w-full">
                      <SelectValue placeholder="Escolha" />
                    </SelectTrigger>
                    <SelectContent>
                      {templates.length === 0 && (
                        <SelectItem value="__none__" disabled>
                          Nenhum template cadastrado
                        </SelectItem>
                      )}
                      {templates.map((t) => (
                        <SelectItem key={t.id} value={t.id}>
                          {t.name} — {t.model_name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>
              <div className="flex flex-col gap-2">
                <Label htmlFor="purchase_date">Data da compra</Label>
                <Input
                  id="purchase_date"
                  name="purchase_date"
                  type="date"
                  required
                  defaultValue={new Date().toISOString().slice(0, 10)}
                />
              </div>
              <div className="flex flex-col gap-2">
                <Label htmlFor="slug">ID do produto</Label>
                <div className="flex items-center gap-2">
                  <Input id="slug" readOnly value={slug} className="font-mono" />
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    onClick={() => setSlug(generateStackSlug())}
                    aria-label="Gerar novo ID"
                  >
                    <RefreshCw />
                  </Button>
                </div>
                <p className="text-xs text-muted-foreground">
                  Será o subdomínio de acesso do cliente.
                </p>
              </div>
              <Button type="button" onClick={toConfirm} disabled={!templateId}>
                Criar stack
              </Button>
            </div>

            {phase === "confirm" && (
              <div className="flex flex-col gap-4">
                {eligible.length > 0 ? (
                  <div className="flex flex-col gap-2">
                    <Label>Máquina de destino</Label>
                    <Select value={machineId} onValueChange={setMachineId}>
                      <SelectTrigger>
                        <SelectValue placeholder="Escolha a máquina" />
                      </SelectTrigger>
                      <SelectContent>
                        {eligible.map((m) => (
                          <SelectItem key={m.id} value={m.id}>
                            {m.name} — {m.model_name}
                            {slotsLabel(m)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <p className="text-xs text-muted-foreground">
                      Máquinas rodando com o template selecionado e vaga livre.
                    </p>
                  </div>
                ) : (
                  <Alert>
                    <TriangleAlert />
                    <AlertTitle>Nenhuma máquina disponível</AlertTitle>
                    <AlertDescription>
                      Será criada uma nova máquina com o template{" "}
                      {template?.name ?? "selecionado"}.
                    </AlertDescription>
                  </Alert>
                )}
                <div className="flex gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    className="flex-1"
                    onClick={() => setPhase("form")}
                    disabled={pending}
                  >
                    Voltar
                  </Button>
                  <Button
                    type="submit"
                    className="flex-1"
                    disabled={pending || (eligible.length > 0 && !machineId)}
                  >
                    {pending
                      ? machineId
                        ? "Criando…"
                        : "Criando máquina… (~1 min)"
                      : "Confirmar"}
                  </Button>
                </div>
              </div>
            )}

            {/* Valores controlados que precisam entrar no FormData */}
            <input type="hidden" name="email" value={email} />
            <input type="hidden" name="plan" value={plan} />
            <input type="hidden" name="template_id" value={templateId} />
            <input type="hidden" name="slug" value={slug} />
            {phase === "confirm" && eligible.length > 0 && (
              <input type="hidden" name="machine_id" value={machineId} />
            )}
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}
