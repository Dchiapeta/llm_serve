"use client"

import * as React from "react"
import { FileText, Trash2, Upload } from "lucide-react"
import { toast } from "sonner"

import { deleteKnowledgeFile, listKnowledgeFiles, uploadKnowledgeFile } from "@/lib/actions"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"

type KnowledgeFile = { storage_path: string; chunks: number }

export function KnowledgeFilesDialog({
  accountId,
  stackId,
  stackSlug,
  initialFiles,
  open,
  onOpenChange,
}: {
  accountId: string
  stackId: string
  stackSlug: string
  initialFiles: KnowledgeFile[]
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  // initialFiles vem do server component (stacks/page.tsx); o remount por
  // key (ver stack-row-actions.tsx) garante que reabrir pega os dados mais
  // recentes da última navegação, sem precisar buscar de novo num effect.
  const [files, setFiles] = React.useState<KnowledgeFile[]>(initialFiles)
  const [pending, startTransition] = React.useTransition()
  const inputRef = React.useRef<HTMLInputElement>(null)

  function reload() {
    listKnowledgeFiles(stackId)
      .then(setFiles)
      .catch((e) => toast.error(e instanceof Error ? e.message : "Erro ao listar arquivos"))
  }

  function onUpload() {
    const file = inputRef.current?.files?.[0]
    if (!file) return
    startTransition(async () => {
      try {
        const formData = new FormData()
        formData.set("account_id", accountId)
        formData.set("stack_id", stackId)
        formData.set("file", file)
        await uploadKnowledgeFile(formData)
        toast.success("Arquivo indexado")
        if (inputRef.current) inputRef.current.value = ""
        reload()
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "Erro ao subir arquivo")
      }
    })
  }

  function onDelete(storagePath: string) {
    startTransition(async () => {
      try {
        await deleteKnowledgeFile(stackId, storagePath)
        toast.success("Arquivo removido")
        reload()
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "Erro ao remover arquivo")
      }
    })
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Base de conhecimento — {stackSlug}</DialogTitle>
          <DialogDescription>
            Arquivos .txt/.md indexados por embedding (OpenAI), usados como
            contexto de RAG nas respostas desta stack.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4">
          <div className="flex gap-2">
            <Input ref={inputRef} type="file" accept=".txt,.md" className="flex-1" />
            <Button onClick={onUpload} disabled={pending}>
              <Upload className="size-4" />
              Subir
            </Button>
          </div>

          <div className="flex flex-col gap-2">
            {files.length === 0 && (
              <p className="text-sm text-muted-foreground">
                Nenhum arquivo enviado ainda.
              </p>
            )}
            {files.map((f) => (
              <div
                key={f.storage_path}
                className="flex items-center justify-between rounded-md border px-3 py-2 text-sm"
              >
                <span className="flex items-center gap-2 truncate">
                  <FileText className="size-4 shrink-0 text-muted-foreground" />
                  <span className="truncate">{f.storage_path.split("/").pop()}</span>
                  <span className="text-xs text-muted-foreground">
                    ({f.chunks} chunk{f.chunks === 1 ? "" : "s"})
                  </span>
                </span>
                <Button
                  variant="ghost"
                  size="icon"
                  disabled={pending}
                  onClick={() => onDelete(f.storage_path)}
                  aria-label={`Remover ${f.storage_path}`}
                >
                  <Trash2 className="size-4" />
                </Button>
              </div>
            ))}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
