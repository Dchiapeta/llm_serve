"use client"

import * as React from "react"
import { Check, Copy, KeyRound, TriangleAlert } from "lucide-react"
import { toast } from "sonner"

import { createKey } from "@/lib/actions"
import type { Account, Machine, TemplatePlan } from "@/lib/types"
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
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"

// Uso do teto de chaves do plano por par conta+máquina, montado no server
// (app/(dashboard)/accounts/page.tsx). limit null = plano sem teto.
export type KeyQuota = { plan: TemplatePlan; used: number; limit: number | null }

export function CreateKeyDialog({
  accounts,
  machines,
  fixedMachineId,
  quotas,
}: {
  accounts: Account[]
  machines: Machine[]
  fixedMachineId?: string
  // Opcional: sem isto o dialog só descobre o limite pelo erro da action, que
  // continua sendo a checagem que vale. É a mesma relação de
  // RAG_FILE_LIMIT_BY_PLAN entre knowledge-files-dialog e assertKnowledgeFileQuota.
  quotas?: Record<string, KeyQuota>
}) {
  const [open, setOpen] = React.useState(false)
  const [accountId, setAccountId] = React.useState<string>("")
  const [machineId, setMachineId] = React.useState<string>(fixedMachineId ?? "")
  const [plainKey, setPlainKey] = React.useState<string | null>(null)
  const [copied, setCopied] = React.useState(false)
  // Nasce LIGADA: quem indexou arquivos na base espera que sejam usados, e a
  // chave nasce sempre explícita (nunca null, o estado legado — ver a coluna
  // enable_knowledge_base na migration 0065).
  const [knowledgeBase, setKnowledgeBase] = React.useState(true)
  const [pending, startTransition] = React.useTransition()

  function reset() {
    setPlainKey(null)
    setCopied(false)
    setAccountId("")
    setKnowledgeBase(true)
    if (!fixedMachineId) setMachineId("")
  }

  function onGenerate() {
    startTransition(async () => {
      try {
        const { plainKey } = await createKey({
          accountId,
          machineId,
          enableKnowledgeBase: knowledgeBase,
        })
        setPlainKey(plainKey)
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "Erro ao gerar chave")
      }
    })
  }

  const quota =
    accountId && machineId ? quotas?.[`${accountId}:${machineId}`] : undefined
  const atLimit = quota != null && quota.limit !== null && quota.used >= quota.limit

  async function copy() {
    if (!plainKey) return
    await navigator.clipboard.writeText(plainKey)
    setCopied(true)
    toast.success("Chave copiada")
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        setOpen(v)
        if (!v) reset()
      }}
    >
      <DialogTrigger asChild>
        <Button variant="outline">
          <KeyRound /> Gerar chave
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Gerar chave de acesso</DialogTitle>
          <DialogDescription>
            Cria uma chave HEX para uma conta usar a LLM de uma máquina.
          </DialogDescription>
        </DialogHeader>

        {plainKey ? (
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
                {plainKey}
              </code>
              <Button variant="outline" size="icon" onClick={copy}>
                {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
              </Button>
            </div>
            <Button onClick={() => setOpen(false)}>Concluir</Button>
          </div>
        ) : (
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-2">
              <Label>Conta</Label>
              <Select value={accountId} onValueChange={setAccountId}>
                <SelectTrigger>
                  <SelectValue placeholder="Escolha a conta" />
                </SelectTrigger>
                <SelectContent>
                  {accounts.map((a) => (
                    <SelectItem key={a.id} value={a.id}>
                      {a.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {!fixedMachineId && (
              <div className="flex flex-col gap-2">
                <Label>Máquina</Label>
                <Select value={machineId} onValueChange={setMachineId}>
                  <SelectTrigger>
                    <SelectValue placeholder="Escolha a máquina" />
                  </SelectTrigger>
                  <SelectContent>
                    {machines.map((m) => (
                      <SelectItem key={m.id} value={m.id}>
                        {m.name} — {m.model_name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}
            {/* Switch, e não Select como os campos acima: os Selects escolhem
                entre N itens (conta, máquina), isto é binário — e é o MESMO
                controle que aparece depois na listagem de chaves
                (KnowledgeBaseToggle), então o admin vê a mesma coisa nos dois
                lugares em vez de um select aqui e um interruptor lá. */}
            <div className="flex items-start justify-between gap-4">
              <div className="flex flex-col gap-1">
                <Label htmlFor="create-key-knowledge-base" className="text-sm font-medium">
                  Base de conhecimento
                </Label>
                <p className="text-xs text-muted-foreground">
                  Ligue em chaves de automação e atendimento (n8n, integrações,
                  chatbots): a cada pergunta o gateway injeta os trechos
                  relevantes dos arquivos indexados da stack. Desligue em chave
                  de CLI de código (Claude Code, Codex) — ali o bloco de
                  contexto muda a cada pergunta e invalida o cache de prefixo do
                  pod, deixando a sessão longa mais lenta sem ajudar.
                </p>
              </div>
              <Switch
                id="create-key-knowledge-base"
                checked={knowledgeBase}
                onCheckedChange={setKnowledgeBase}
                disabled={pending}
              />
            </div>
            {quota && (
              <p
                className={
                  atLimit ? "text-sm text-destructive" : "text-sm text-muted-foreground"
                }
              >
                {quota.used}
                {quota.limit === null ? "" : ` / ${quota.limit}`} chave(s) ativa(s)
                {quota.limit === null
                  ? ` — plano ${quota.plan} não tem limite`
                  : ` do plano ${quota.plan}`}
                {atLimit && ". Revogue uma chave para emitir outra."}
              </p>
            )}
            <Button
              onClick={onGenerate}
              disabled={pending || !accountId || !machineId || atLimit}
            >
              {pending ? "Gerando…" : "Gerar chave HEX"}
            </Button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
