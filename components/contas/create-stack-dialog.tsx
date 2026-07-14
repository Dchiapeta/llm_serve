"use client"

import * as React from "react"
import { Plus, RefreshCw } from "lucide-react"
import { toast } from "sonner"

import { createStack } from "@/lib/actions"
import { generateStackSlug } from "@/lib/slug"
import { TEMPLATE_PLANS, type Account, type TemplatePlan } from "@/lib/types"
import {
  Autocomplete,
  AutocompleteContent,
  AutocompleteEmpty,
  AutocompleteInput,
  AutocompleteItem,
  AutocompleteList,
} from "@/components/reui/autocomplete"
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

export function CreateStackDialog({
  accounts,
}: {
  accounts: Pick<Account, "id" | "name" | "email">[]
}) {
  const [open, setOpen] = React.useState(false)
  const [email, setEmail] = React.useState("")
  const [name, setName] = React.useState("")
  const [plan, setPlan] = React.useState<TemplatePlan>("VibeCoder")
  const [slug, setSlug] = React.useState(generateStackSlug)
  const [pending, startTransition] = React.useTransition()
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

  function onOpenChange(next: boolean) {
    setOpen(next)
    if (next) {
      setEmail("")
      setName("")
      setPlan("VibeCoder")
      setSlug(generateStackSlug())
    }
  }

  function onEmailChange(value: string) {
    setEmail(value)
    const match = accounts.find(
      (a) => a.email?.toLowerCase() === value.trim().toLowerCase()
    )
    if (match) setName(match.name)
  }

  function onSubmit(formData: FormData) {
    startTransition(async () => {
      try {
        const { slug: finalSlug } = await createStack(formData)
        toast.success(`Stack criada — ID: ${finalSlug}`)
        setOpen(false)
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "Erro ao criar stack")
      }
    })
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogTrigger asChild>
        <Button>
          <Plus /> Nova stack
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-sm" ref={contentRef}>
        <DialogHeader>
          <DialogTitle>Nova stack</DialogTitle>
          <DialogDescription>
            Uma stack é uma LLM contratada por um cliente. Se o e-mail já
            tiver conta, a stack é adicionada a ela.
          </DialogDescription>
        </DialogHeader>
        <form action={onSubmit} className="flex flex-col gap-4">
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
            <input type="hidden" name="email" value={email} />
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
              <SelectTrigger>
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
            <input type="hidden" name="plan" value={plan} />
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
            <input type="hidden" name="slug" value={slug} />
            <p className="text-xs text-muted-foreground">
              Será o subdomínio de acesso do cliente.
            </p>
          </div>
          <Button type="submit" disabled={pending}>
            {pending ? "Criando…" : "Criar stack"}
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  )
}
