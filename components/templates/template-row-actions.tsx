"use client"

import * as React from "react"
import { Info, MoreHorizontal, Pencil, Trash2 } from "lucide-react"
import { toast } from "sonner"

import { deleteTemplate } from "@/lib/actions"
import type { GpuType } from "@/lib/runpod"
import type { Machine, Template } from "@/lib/types"
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { EditTemplateDialog } from "@/components/templates/edit-template-dialog"
import { TemplateInfoDialog } from "@/components/templates/template-info-dialog"

export function TemplateRowActions({
  template,
  gpus,
  machines,
}: {
  template: Template
  gpus: GpuType[]
  machines: Machine[]
}) {
  const [infoOpen, setInfoOpen] = React.useState(false)
  const [editOpen, setEditOpen] = React.useState(false)
  const [deleteOpen, setDeleteOpen] = React.useState(false)
  const [pending, startTransition] = React.useTransition()

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            aria-label={`Ações para ${template.name}`}
          >
            <MoreHorizontal className="size-4" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onSelect={() => setInfoOpen(true)}>
            <Info className="size-4" />
            Info
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => setEditOpen(true)}>
            <Pencil className="size-4" />
            Editar
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

      <TemplateInfoDialog
        template={template}
        machines={machines}
        open={infoOpen}
        onOpenChange={setInfoOpen}
      />

      <EditTemplateDialog
        template={template}
        gpus={gpus}
        open={editOpen}
        onOpenChange={setEditOpen}
      />

      <AlertDialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Apagar template?</AlertDialogTitle>
            <AlertDialogDescription>
              O template “{template.name}” será removido do painel e do RunPod.
              Máquinas já criadas não são afetadas.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancelar</AlertDialogCancel>
            <AlertDialogAction
              disabled={pending}
              onClick={(e) => {
                e.preventDefault()
                startTransition(async () => {
                  try {
                    await deleteTemplate(template.id)
                    toast.success("Produto apagado")
                    setDeleteOpen(false)
                  } catch (err) {
                    toast.error(
                      err instanceof Error ? err.message : "Erro ao apagar"
                    )
                  }
                })
              }}
            >
              Apagar
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  )
}
