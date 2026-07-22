import { listGpuTypes, type GpuType } from "@/lib/runpod"
import { CreateTemplateDialog } from "@/components/templates/create-template-dialog"

// Botão "Criar produto". Vive no seu próprio <Suspense> para não segurar o
// cabeçalho da página; gpus vem do cache (unstable_cache em listGpuTypes).
export async function TemplatesToolbar() {
  let gpus: GpuType[] = []
  try {
    gpus = await listGpuTypes()
  } catch {
    // sem GPUs, o dialog mostra aviso
  }
  return <CreateTemplateDialog gpus={gpus} />
}
