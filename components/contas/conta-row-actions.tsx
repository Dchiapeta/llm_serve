"use client"

import * as React from "react"
import { Info, MoreVertical, Trash2 } from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { ContaDetalhesDialog } from "@/components/contas/conta-detalhes-dialog"
import { DeleteContaDialog } from "@/components/contas/delete-conta-dialog"
import type { UsuarioRow } from "@/components/contas/usuarios-table"

export function ContaRowActions({ conta }: { conta: UsuarioRow }) {
  const [infoOpen, setInfoOpen] = React.useState(false)
  const [deleteOpen, setDeleteOpen] = React.useState(false)

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            aria-label={`Ações para ${conta.name}`}
          >
            <MoreVertical className="size-4" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onSelect={() => setInfoOpen(true)}>
            <Info className="size-4" />
            Info
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            variant="destructive"
            onSelect={() => setDeleteOpen(true)}
          >
            <Trash2 className="size-4" />
            Deletar
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <ContaDetalhesDialog
        conta={conta}
        open={infoOpen}
        onOpenChange={setInfoOpen}
      />

      <DeleteContaDialog
        conta={conta}
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
      />
    </>
  )
}
