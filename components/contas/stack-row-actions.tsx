"use client"

import * as React from "react"
import {
  ArrowRightLeft,
  BookOpen,
  Info,
  MoreVertical,
  Settings,
  Trash2,
  UserRound,
} from "lucide-react"

import type { Machine } from "@/lib/types"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { ContaInfoDialog } from "@/components/contas/conta-info-dialog"
import type { StackRow } from "@/components/contas/contas-table"
import type {
  StackMachine,
  StackTemplate,
} from "@/components/contas/create-stack-dialog"
import { DeleteStackDialog } from "@/components/contas/delete-stack-button"
import { EditAccountConfigDialog } from "@/components/contas/edit-account-config-dialog"
import { KnowledgeFilesDialog } from "@/components/contas/knowledge-files-dialog"
import { MigrateAccountDialog } from "@/components/contas/migrate-account-dialog"
import { MigrateStackDialog } from "@/components/contas/migrate-stack-dialog"
import { StackInfoDialog } from "@/components/contas/stack-info-dialog"

export function StackRowActions({
  row,
  periodLabel,
  runningMachines,
  stackMachines,
  templates,
}: {
  row: StackRow
  periodLabel: string
  runningMachines: Machine[]
  stackMachines: StackMachine[]
  templates: StackTemplate[]
}) {
  const { stack, account, route, currentMachine, hasReadyAdapter, knowledgeFiles } = row

  const [stackInfoOpen, setStackInfoOpen] = React.useState(false)
  const [migrateStackOpen, setMigrateStackOpen] = React.useState(false)
  const [deleteStackOpen, setDeleteStackOpen] = React.useState(false)
  const [contaInfoOpen, setContaInfoOpen] = React.useState(false)
  const [configOpen, setConfigOpen] = React.useState(false)
  const [knowledgeOpen, setKnowledgeOpen] = React.useState(false)
  const [migrateAccountOpen, setMigrateAccountOpen] = React.useState(false)

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            aria-label={`Ações para ${stack.slug}`}
          >
            <MoreVertical className="size-4" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-56">
          <DropdownMenuLabel>Stack</DropdownMenuLabel>
          <DropdownMenuItem onSelect={() => setStackInfoOpen(true)}>
            <Info className="size-4" />
            Info
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => setConfigOpen(true)}>
            <Settings className="size-4" />
            System prompt
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => setKnowledgeOpen(true)}>
            <BookOpen className="size-4" />
            Base de conhecimento
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => setMigrateStackOpen(true)}>
            <ArrowRightLeft className="size-4" />
            Migrar
          </DropdownMenuItem>
          <DropdownMenuItem
            variant="destructive"
            onSelect={() => setDeleteStackOpen(true)}
          >
            <Trash2 className="size-4" />
            Deletar
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuLabel>Conta</DropdownMenuLabel>
          <DropdownMenuItem onSelect={() => setContaInfoOpen(true)}>
            <UserRound className="size-4" />
            Info da conta
          </DropdownMenuItem>
          <DropdownMenuItem
            disabled={!hasReadyAdapter}
            onSelect={() => setMigrateAccountOpen(true)}
          >
            <ArrowRightLeft className="size-4" />
            Migrar conta
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <StackInfoDialog
        stack={stack}
        periodLabel={periodLabel}
        open={stackInfoOpen}
        onOpenChange={setStackInfoOpen}
      />

      {/* key força remount a cada abertura: reseta alvo/resultado da
          migração anterior sem sincronizar via effect */}
      <MigrateStackDialog
        key={migrateStackOpen ? "migrate-stack-open" : "migrate-stack-closed"}
        stack={stack}
        machines={stackMachines}
        templates={templates}
        open={migrateStackOpen}
        onOpenChange={setMigrateStackOpen}
      />

      <DeleteStackDialog
        stackId={stack.id}
        slug={stack.slug}
        open={deleteStackOpen}
        onOpenChange={setDeleteStackOpen}
      />

      <ContaInfoDialog
        account={account}
        route={route}
        currentMachineName={currentMachine?.name}
        open={contaInfoOpen}
        onOpenChange={setContaInfoOpen}
      />

      {/* key força remount a cada abertura: reseta o form pros valores
          atuais da stack em vez de arrastar o que sobrou de uma edição
          anterior cancelada, sem precisar sincronizar via effect */}
      <EditAccountConfigDialog
        key={configOpen ? "config-open" : "config-closed"}
        stack={stack}
        open={configOpen}
        onOpenChange={setConfigOpen}
      />

      <KnowledgeFilesDialog
        key={knowledgeOpen ? "knowledge-open" : "knowledge-closed"}
        accountId={account.id}
        stackId={stack.id}
        stackSlug={stack.slug}
        initialFiles={knowledgeFiles}
        open={knowledgeOpen}
        onOpenChange={setKnowledgeOpen}
      />

      <MigrateAccountDialog
        accountId={account.id}
        accountName={account.name}
        currentMachineName={currentMachine?.name}
        eligibleMachines={runningMachines.filter(
          (m) => m.id !== route?.machine_id
        )}
        open={migrateAccountOpen}
        onOpenChange={setMigrateAccountOpen}
      />
    </>
  )
}
