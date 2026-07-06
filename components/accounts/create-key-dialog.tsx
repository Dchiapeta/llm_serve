"use client"

import * as React from "react"
import { Check, Copy, KeyRound, TriangleAlert } from "lucide-react"
import { toast } from "sonner"

import { createKey } from "@/lib/actions"
import type { Account, Machine } from "@/lib/types"
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

export function CreateKeyDialog({
  accounts,
  machines,
  fixedMachineId,
}: {
  accounts: Account[]
  machines: Machine[]
  fixedMachineId?: string
}) {
  const [open, setOpen] = React.useState(false)
  const [accountId, setAccountId] = React.useState<string>("")
  const [machineId, setMachineId] = React.useState<string>(fixedMachineId ?? "")
  const [plainKey, setPlainKey] = React.useState<string | null>(null)
  const [copied, setCopied] = React.useState(false)
  const [pending, startTransition] = React.useTransition()

  function reset() {
    setPlainKey(null)
    setCopied(false)
    setAccountId("")
    if (!fixedMachineId) setMachineId("")
  }

  function onGenerate() {
    startTransition(async () => {
      try {
        const { plainKey } = await createKey({ accountId, machineId })
        setPlainKey(plainKey)
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "Erro ao gerar chave")
      }
    })
  }

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
            <Button
              onClick={onGenerate}
              disabled={pending || !accountId || !machineId}
            >
              {pending ? "Gerando…" : "Gerar chave HEX"}
            </Button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
