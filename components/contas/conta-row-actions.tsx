"use client"

import * as React from "react"
import { ArrowRightLeft, BookOpen, Info, MoreVertical, Settings } from "lucide-react"

import type { Account, Machine, RoutingState } from "@/lib/types"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { ContaInfoDialog } from "@/components/contas/conta-info-dialog"
import { EditAccountConfigDialog } from "@/components/contas/edit-account-config-dialog"
import { KnowledgeFilesDialog } from "@/components/contas/knowledge-files-dialog"
import { MigrateAccountDialog } from "@/components/contas/migrate-account-dialog"

export function ContaRowActions({
  account,
  route,
  currentMachineName,
  eligibleMachines,
  hasReadyAdapter,
  knowledgeFiles,
}: {
  account: Account
  route: RoutingState | undefined
  currentMachineName: string | undefined
  eligibleMachines: Machine[]
  hasReadyAdapter: boolean
  knowledgeFiles: { storage_path: string; chunks: number }[]
}) {
  const [infoOpen, setInfoOpen] = React.useState(false)
  const [configOpen, setConfigOpen] = React.useState(false)
  const [knowledgeOpen, setKnowledgeOpen] = React.useState(false)
  const [migrateOpen, setMigrateOpen] = React.useState(false)

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            aria-label={`Ações para ${account.name}`}
          >
            <MoreVertical className="size-4" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onSelect={() => setInfoOpen(true)}>
            <Info className="size-4" />
            Info
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => setConfigOpen(true)}>
            <Settings className="size-4" />
            Plano / system prompt
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => setKnowledgeOpen(true)}>
            <BookOpen className="size-4" />
            Base de conhecimento
          </DropdownMenuItem>
          <DropdownMenuItem
            disabled={!hasReadyAdapter}
            onSelect={() => setMigrateOpen(true)}
          >
            <ArrowRightLeft className="size-4" />
            Migrar
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <ContaInfoDialog
        account={account}
        route={route}
        currentMachineName={currentMachineName}
        open={infoOpen}
        onOpenChange={setInfoOpen}
      />

      {/* key força remount a cada abertura: reseta o form pros valores
          atuais da conta em vez de arrastar o que sobrou de uma edição
          anterior cancelada, sem precisar sincronizar via effect */}
      <EditAccountConfigDialog
        key={configOpen ? "config-open" : "config-closed"}
        account={account}
        open={configOpen}
        onOpenChange={setConfigOpen}
      />

      <KnowledgeFilesDialog
        key={knowledgeOpen ? "knowledge-open" : "knowledge-closed"}
        accountId={account.id}
        accountName={account.name}
        initialFiles={knowledgeFiles}
        open={knowledgeOpen}
        onOpenChange={setKnowledgeOpen}
      />

      <MigrateAccountDialog
        accountId={account.id}
        accountName={account.name}
        currentMachineName={currentMachineName}
        eligibleMachines={eligibleMachines}
        open={migrateOpen}
        onOpenChange={setMigrateOpen}
      />
    </>
  )
}
