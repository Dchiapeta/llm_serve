"use client"

import * as React from "react"
import { Check, Copy, Plus, RefreshCw, Server, TriangleAlert } from "lucide-react"
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
import { Badge } from "@/components/ui/badge"
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
import { Progress } from "@/components/ui/progress"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

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
  // stacks hospedadas na máquina (1 stack = 1 slot)
  occupied: number
}

type Phase = "form" | "confirm" | "done"

// Sentinela do select de destino: provisiona uma máquina nova em vez de usar
// uma existente (Radix Select não aceita item com value vazio).
const NEW_MACHINE = "__new__"

// Capacidade de uma máquina segundo o template dela (mesma conta do painel
// de máquinas).
function machineCapacity(
  m: StackMachine,
  tpl: Pick<StackTemplate, "model_footprint_gb" | "kv_reserve_gb_per_user"> | undefined
) {
  return computeCapacity({
    vramGb: m.vram_gb,
    modelFootprintGb: tpl?.model_footprint_gb ?? 16,
    kvReserveGbPerUser: tpl?.kv_reserve_gb_per_user ?? 2,
    occupied: m.occupied,
    maxUsers: m.max_users,
  })
}

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

  // O produto escolhido (VibeCoder/Pro/Max/Enterprise) determina o template:
  // o cadastrado em /templates com aquele plano.
  const template = templates.find((t) => t.plan === plan)
  const templateId = template?.id ?? ""

  // Máquinas elegíveis: rodando, do template escolhido, com slot livre —
  // mesma semântica de createKey (slotsMax 0 = desconhecida, não bloqueia).
  const eligible = React.useMemo(() => {
    if (!template) return []
    return machines.filter((m) => {
      if (m.template_id !== template.id) return false
      const cap = machineCapacity(m, template)
      return !(cap.slotsMax > 0 && cap.slotsUsed >= cap.slotsMax)
    })
  }, [machines, template])

  function slotsLabel(m: StackMachine) {
    if (!template) return ""
    const cap = machineCapacity(m, template)
    return cap.slotsMax > 0 ? ` (${cap.slotsFree} vaga(s))` : ""
  }

  function onOpenChange(next: boolean) {
    setOpen(next)
    if (next) {
      setPhase("form")
      setEmail("")
      setName("")
      setPlan("VibeCoder")
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
    if (!template) {
      toast.error(`Nenhum produto ${plan} cadastrado`)
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
              <div className="flex flex-col gap-2">
                <Label>Produto</Label>
                <Select value={plan} onValueChange={(v) => setPlan(v as TemplatePlan)}>
                  <SelectTrigger className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {TEMPLATE_PLANS.map((p) => (
                      <SelectItem
                        key={p}
                        value={p}
                        disabled={!templates.some((t) => t.plan === p)}
                      >
                        {p}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                {template ? (
                  <p className="text-xs text-muted-foreground">
                    Modelo: <span className="font-mono">{template.model_name}</span>
                  </p>
                ) : (
                  <p className="text-xs text-destructive">
                    Nenhum produto {plan} cadastrado. Cadastre em Produtos.
                  </p>
                )}
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
                    <div className="flex items-center justify-between">
                      <Label>Máquina de destino</Label>
                      <MachineSlotsDialog
                        machines={machines}
                        templates={templates}
                        currentTemplateId={template?.id}
                      />
                    </div>
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
                        <SelectSeparator />
                        <SelectItem value={NEW_MACHINE}>
                          <Plus className="size-4" /> Criar nova máquina
                        </SelectItem>
                      </SelectContent>
                    </Select>
                    <p className="text-xs text-muted-foreground">
                      {machineId === NEW_MACHINE
                        ? `Uma máquina nova será provisionada com o produto ${template?.name ?? "selecionado"} (~1 min).`
                        : "Máquinas rodando com o produto selecionado e vaga livre."}
                    </p>
                  </div>
                ) : (
                  <>
                    <Alert>
                      <TriangleAlert />
                      <AlertTitle>Nenhuma máquina disponível</AlertTitle>
                      <AlertDescription>
                        Será criada uma nova máquina com o produto{" "}
                        {template?.name ?? "selecionado"}.
                      </AlertDescription>
                    </Alert>
                    <MachineSlotsDialog
                      machines={machines}
                      templates={templates}
                      currentTemplateId={template?.id}
                    />
                  </>
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
                      ? machineId && machineId !== NEW_MACHINE
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
              <input
                type="hidden"
                name="machine_id"
                // vazio = createStack provisiona uma máquina nova
                value={machineId === NEW_MACHINE ? "" : machineId}
              />
            )}
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}

// Visão geral das máquinas rodando e seus slots livres, para o admin decidir
// entre reaproveitar uma existente ou criar uma nova. Dialog aninhado ao de
// criação de stack (o Radix empilha modais sem conflito de pointer-events).
function MachineSlotsDialog({
  machines,
  templates,
  currentTemplateId,
}: {
  machines: StackMachine[]
  templates: StackTemplate[]
  currentTemplateId: string | undefined
}) {
  const templateById = new Map(templates.map((t) => [t.id, t]))
  // Máquinas do produto selecionado primeiro, depois por nome.
  const sorted = [...machines].sort((a, b) => {
    const aCurrent = a.template_id === currentTemplateId ? 0 : 1
    const bCurrent = b.template_id === currentTemplateId ? 0 : 1
    return aCurrent - bCurrent || a.name.localeCompare(b.name)
  })

  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button type="button" variant="outline" size="sm">
          <Server className="size-4" /> Ver máquinas e slots
        </Button>
      </DialogTrigger>
      {/* `!` porque .style-nova .cn-dialog-content (sm:max-w-sm) tem
          especificidade maior que utilitários — mesmo caso do StatusBadge. */}
      <DialogContent className="sm:max-w-4xl!">
        <DialogHeader>
          <DialogTitle>Máquinas e slots disponíveis</DialogTitle>
          <DialogDescription>
            Máquinas rodando e quantas vagas cada uma ainda tem. Apenas as do
            produto selecionado podem hospedar esta stack.
          </DialogDescription>
        </DialogHeader>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Nome</TableHead>
              <TableHead>Produto</TableHead>
              <TableHead>Modelo</TableHead>
              <TableHead>Slots</TableHead>
              <TableHead className="text-right">Vagas</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {sorted.length === 0 && (
              <TableRow>
                <TableCell colSpan={5} className="text-center text-muted-foreground">
                  Nenhuma máquina rodando.
                </TableCell>
              </TableRow>
            )}
            {sorted.map((m) => {
              const tpl = templateById.get(m.template_id)
              const cap = machineCapacity(m, tpl)
              const isCurrent = m.template_id === currentTemplateId
              return (
                <TableRow key={m.id}>
                  <TableCell className="font-medium">
                    <span className="flex items-center gap-2">
                      {m.name}
                      {isCurrent && <Badge variant="outline">produto atual</Badge>}
                    </span>
                  </TableCell>
                  <TableCell className="text-sm">{tpl?.name ?? "—"}</TableCell>
                  <TableCell className="font-mono text-xs">{m.model_name}</TableCell>
                  <TableCell>
                    <div className="flex items-center gap-2">
                      <Progress value={cap.usagePct} className="w-24" />
                      <span className="text-xs text-muted-foreground">
                        {cap.slotsUsed}/{cap.slotsMax}
                      </span>
                    </div>
                  </TableCell>
                  <TableCell className="text-right text-sm">
                    {cap.slotsMax > 0 ? cap.slotsFree : "—"}
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </DialogContent>
    </Dialog>
  )
}
