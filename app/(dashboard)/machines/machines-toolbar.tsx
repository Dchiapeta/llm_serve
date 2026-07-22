import { listGpuTypes } from "@/lib/runpod"
import { CreateMachineDialog } from "@/components/machines/create-machine-dialog"

import { getTemplates } from "./queries"

// Botão "Criar máquina". Vive no seu próprio <Suspense> para não segurar o
// cabeçalho da página: título/descrição já estão no shell estático.
export async function MachinesToolbar() {
  const [templates, gpus] = await Promise.all([
    getTemplates(),
    listGpuTypes().catch(() => []),
  ])
  return <CreateMachineDialog templates={templates} gpus={gpus} />
}
