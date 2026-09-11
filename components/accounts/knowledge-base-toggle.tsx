"use client"

import * as React from "react"
import { toast } from "sonner"

import { setKeyKnowledgeBase } from "@/lib/actions"
import { NativeSelect } from "@/components/ui/native-select"

// Base de conhecimento (RAG da stack) por CHAVE — migration 0065.
//
// Três estados no banco e TRÊS no controle, ao contrário de um Switch:
//   null  = automático — o gateway só monta o bloco de contexto quando a
//           request não traz `system` próprio (build_stack_system_message).
//   true  = consulta sempre · false = nunca consulta
//
// O Switch foi tentado e não serve aqui. Com o legado desenhado como ligado
// (que é a verdade: sem `system` do cliente a base É consultada), o único
// clique possível numa chave legada grava `false` — ou seja, dava para fixar
// "desligada" mas nunca "ligada", que é justamente o que se quer numa chave de
// n8n. Um controle de dois estados não representa três, e o intermediário
// gravaria no banco o oposto da intenção. Mesmo tri-estado do seletor de
// raciocínio em edit-account-config-dialog.tsx.
const OPTIONS = [
  { value: "inherit", label: "automático" },
  { value: "on", label: "ligada" },
  { value: "off", label: "desligada" },
] as const

const TITLES: Record<string, string> = {
  inherit:
    "Comportamento legado: a base só é consultada quando a requisição não manda " +
    "instruções de sistema próprias.",
  on:
    "A base é consultada em toda pergunta desta chave, mesmo quando o cliente " +
    "manda instruções de sistema próprias (o caso do n8n).",
  off: "A base nunca é consultada nesta chave.",
}

function toOption(value: boolean | null | undefined) {
  return value == null ? "inherit" : value ? "on" : "off"
}

export function KnowledgeBaseToggle({
  keyId,
  initial,
}: {
  keyId: string
  initial?: boolean | null
}) {
  const [value, setValue] = React.useState(() => toOption(initial))
  const [pending, startTransition] = React.useTransition()

  // A linha da tabela não remonta entre revalidações (a `key` é o id da chave),
  // então sem isto o seletor continuaria mostrando o valor velho depois de uma
  // escrita vinda de fora — o cliente mexendo no painel dele, ou outra aba do
  // admin. Alinha o estado local sempre que o servidor mandar um valor novo.
  React.useEffect(() => {
    setValue(toOption(initial))
  }, [initial])

  function onChange(next: string) {
    const previous = value
    setValue(next)
    startTransition(async () => {
      try {
        await setKeyKnowledgeBase(keyId, next === "inherit" ? null : next === "on")
        toast.success("Base de conhecimento atualizada nesta chave")
      } catch (e) {
        setValue(previous)
        toast.error(e instanceof Error ? e.message : "Erro ao salvar")
      }
    })
  }

  return (
    <NativeSelect
      aria-label="Base de conhecimento"
      title={TITLES[value]}
      className="h-7 w-auto py-0 text-xs"
      value={value}
      disabled={pending}
      onChange={(e) => onChange(e.target.value)}
    >
      {OPTIONS.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </NativeSelect>
  )
}
