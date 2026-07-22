import { machineDisplayStatus } from "@/lib/machines"
import type { Machine } from "@/lib/types"
import { StatusBadge } from "@/components/machines/status-badge"

// Sonda o /health público do agent (até 3s) para refinar "Rodando"/"Subindo"/
// "Falha". Fica FORA do caminho de render da tabela: cada badge é envolvido num
// <Suspense> cujo fallback é o status do banco, então a lista aparece na hora e
// os badges refinam quando o health responde — nenhum pod lento prende a página.
export async function LiveStatusBadge({ machine }: { machine: Machine }) {
  const status = await machineDisplayStatus(machine)
  return <StatusBadge status={status} />
}
