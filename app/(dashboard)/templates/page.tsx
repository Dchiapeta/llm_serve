import { createSupabaseAdmin } from "@/lib/supabase/server"
import type { Template } from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { CreateTemplateDialog } from "@/components/templates/create-template-dialog"
import { DeleteTemplateButton } from "@/components/templates/delete-template-button"

export const dynamic = "force-dynamic"

export default async function TemplatesPage() {
  const db = createSupabaseAdmin()
  const { data } = await db
    .from("templates")
    .select("*")
    .order("created_at", { ascending: false })
  const templates = (data ?? []) as Template[]

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Templates</h1>
          <p className="text-sm text-muted-foreground">
            Configurações de imagem e modelo para criar máquinas
          </p>
        </div>
        <CreateTemplateDialog />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Todos os templates</CardTitle>
          <CardDescription>{templates.length} template(s)</CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Nome</TableHead>
                <TableHead>Modelo</TableHead>
                <TableHead>Imagem</TableHead>
                <TableHead>Disco</TableHead>
                <TableHead>Capacidade</TableHead>
                <TableHead>RunPod</TableHead>
                <TableHead className="w-12" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {templates.length === 0 && (
                <TableRow>
                  <TableCell colSpan={7} className="text-center text-muted-foreground">
                    Nenhum template ainda. Crie o primeiro.
                  </TableCell>
                </TableRow>
              )}
              {templates.map((t) => (
                <TableRow key={t.id}>
                  <TableCell className="font-medium">{t.name}</TableCell>
                  <TableCell className="font-mono text-xs">{t.model_name}</TableCell>
                  <TableCell className="font-mono text-xs">{t.image}</TableCell>
                  <TableCell>{t.disk_gb} GB</TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {t.model_footprint_gb} GB + {t.kv_reserve_gb_per_user} GB/usuário
                  </TableCell>
                  <TableCell>
                    {t.runpod_template_id ? (
                      <Badge variant="secondary">sincronizado</Badge>
                    ) : (
                      <Badge variant="outline">só local</Badge>
                    )}
                  </TableCell>
                  <TableCell>
                    <DeleteTemplateButton id={t.id} name={t.name} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  )
}
