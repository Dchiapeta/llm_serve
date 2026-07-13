"use client"

import * as React from "react"
import { ArrowRightLeft, Info, MoreVertical } from "lucide-react"

import type { Account, Machine, RoutingState } from "@/lib/types"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { ContaInfoDialog } from "@/components/contas/conta-info-dialog"
import { MigrateAccountDialog } from "@/components/contas/migrate-account-dialog"

export function ContaRowActions({
  account,
  route,
  currentMachineName,
  plan,
  eligibleMachines,
  hasReadyAdapter,
}: {
  account: Account
  route: RoutingState | undefined
  currentMachineName: string | undefined
  plan: "Básico" | "Avançado"
  eligibleMachines: Machine[]
  hasReadyAdapter: boolean
}) {
  const [infoOpen, setInfoOpen] = React.useState(false)
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
        plan={plan}
        open={infoOpen}
        onOpenChange={setInfoOpen}
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
