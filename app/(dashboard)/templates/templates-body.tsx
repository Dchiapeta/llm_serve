import { createSupabaseAdmin } from "@/lib/supabase/server"
import { listGpuTypes, runpod, type GpuType, type RunPodTemplate } from "@/lib/runpod"
import type { Machine, Template } from "@/lib/types"
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
import { TemplateRowActions } from "@/components/templates/template-row-actions"
import { ImportTemplateDialog } from "@/components/templates/import-template-dialog"

// Corpo da página de produtos: tabela local + o que existe só no RunPod. A
// chamada externa runpod.listTemplates() fica aqui (sob <Suspense>), então o
// cabeçalho da página aparece sem esperar o RunPod.
export async function TemplatesBody() {
  const db = createSupabaseAdmin()
  const [{ data }, { data: machinesData }] = await Promise.all([
    db
      .from("templates")
      .select("*")
      .order("created_at", { ascending: false }),
    db.from("machines").select("*").order("created_at", { ascending: false }),
  ])
  const templates = (data ?? []) as Template[]

  // template_id → máquinas que usam esse produto (relação 1:N)
  const machinesByTemplate = new Map<string, Machine[]>()
  for (const m of (machinesData ?? []) as Machine[]) {
    if (!m.template_id) continue
    const list = machinesByTemplate.get(m.template_id) ?? []
    list.push(m)
    machinesByTemplate.set(m.template_id, list)
  }

  // templates que existem no RunPod mas ainda não foram importados localmente
  let remoteTemplates: RunPodTemplate[] = []
  let runpodError: string | null = null
  try {
    remoteTemplates = await runpod.listTemplates()
  } catch (e) {
    runpodError = e instanceof Error ? e.message : "Falha ao consultar o RunPod"
  }
  const importedIds = new Set(
    templates.map((t) => t.runpod_template_id).filter(Boolean)
  )
  const notImported = remoteTemplates.filter((t) => !importedIds.has(t.id))

  let gpus: GpuType[] = []
  try {
    gpus = await listGpuTypes()
  } catch {
    // sem GPUs, o dialog mostra aviso
  }

  return (
    <>
      {runpodError && (
        <p className="text-sm text-destructive">
          Não foi possível consultar o RunPod: {runpodError}
        </p>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Todos os produtos</CardTitle>
          <CardDescription>
            {templates.length} local(is)
            {notImported.length > 0 &&
              ` · ${notImported.length} no RunPod para importar`}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Nome</TableHead>
                <TableHead>Plano</TableHead>
                <TableHead>Modelo</TableHead>
                <TableHead>Imagem</TableHead>
                <TableHead>Disco</TableHead>
                <TableHead>Env vars</TableHead>
                <TableHead>Capacidade</TableHead>
                <TableHead>Origem</TableHead>
                <TableHead className="w-28" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {templates.length === 0 && notImported.length === 0 && (
                <TableRow>
                  <TableCell colSpan={9} className="text-center text-muted-foreground">
                    Nenhum produto ainda. Crie o primeiro.
                  </TableCell>
                </TableRow>
              )}

              {templates.map((t) => (
                <TableRow key={t.id}>
                  <TableCell className="font-medium">{t.name}</TableCell>
                  <TableCell>
                    <Badge variant="outline">{t.plan}</Badge>
                  </TableCell>
                  <TableCell className="font-mono text-xs">{t.model_name}</TableCell>
                  <TableCell className="font-mono text-xs">{t.image}</TableCell>
                  <TableCell>{t.disk_gb} GB</TableCell>
                  <TableCell
                    className="max-w-40 truncate font-mono text-xs text-muted-foreground"
                    title={Object.keys(t.env ?? {}).join(", ")}
                  >
                    {Object.keys(t.env ?? {}).length > 0
                      ? Object.keys(t.env).join(", ")
                      : "—"}
                  </TableCell>
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
                  <TableCell className="text-right">
                    <TemplateRowActions
                      template={t}
                      gpus={gpus}
                      machines={machinesByTemplate.get(t.id) ?? []}
                    />
                  </TableCell>
                </TableRow>
              ))}

              {notImported.map((t) => (
                <TableRow key={t.id} className="text-muted-foreground">
                  <TableCell className="font-medium">{t.name}</TableCell>
                  <TableCell className="text-xs">—</TableCell>
                  <TableCell className="text-xs">—</TableCell>
                  <TableCell className="font-mono text-xs">{t.imageName}</TableCell>
                  <TableCell>{t.containerDiskInGb ?? "—"} GB</TableCell>
                  <TableCell
                    className="max-w-40 truncate font-mono text-xs"
                    title={Object.keys(t.env ?? {}).join(", ")}
                  >
                    {Object.keys(t.env ?? {}).length > 0
                      ? Object.keys(t.env ?? {}).join(", ")
                      : "—"}
                  </TableCell>
                  <TableCell className="text-xs">—</TableCell>
                  <TableCell>
                    <Badge variant="outline">só no RunPod</Badge>
                  </TableCell>
                  <TableCell>
                    <ImportTemplateDialog
                      runpodTemplateId={t.id}
                      name={t.name}
                      image={t.imageName}
                      gpus={gpus}
                    />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </>
  )
}
