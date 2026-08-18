// Apresentação do estado de cobrança da stack (migration 0050).
//
// Extraído de components/contas/contas-table.tsx para o CRM não reimplementar
// a mesma regra: a data do corte é o número que o suporte usa para decidir se
// cobra ou espera, e duas versões dela divergiriam em silêncio.

import { BILLING_GRACE_HOURS, type BillingStatus } from "./types"

export type BillingBadge = {
  label: string
  variant:
    | "warning-light"
    | "info-light"
    | "destructive-light"
    | "outline"
    | "success-light"
}

// 'active' não ganha badge: é o caso normal e poluiria a tabela inteira — a
// coluna só chama atenção pro que exige ação.
export const BILLING_BADGE: Partial<Record<BillingStatus, BillingBadge>> = {
  trialing: { label: "Trial", variant: "info-light" },
  past_due: { label: "Em atraso", variant: "warning-light" },
  suspended: { label: "Suspensa", variant: "destructive-light" },
  canceled: { label: "Cancelada", variant: "outline" },
}

/**
 * Quanto falta da tolerância antes do corte. Vencido = a stack está bloqueada
 * de fato mesmo que o loop ainda não a tenha marcado como 'suspended' (janela
 * de até 60s entre uma coisa e outra).
 *
 * O `status` é parâmetro, e não responsabilidade de quem chama, porque
 * `suspend_stack` NÃO limpa `past_due_since`: uma stack já suspensa continua
 * com a data preenchida e, sem esta guarda, a tela exibiria "Suspensa · corta
 * em 70h" — prometendo prazo a um cliente que já foi cortado.
 *
 * `ceil` e não `floor`: com 50 min restando, floor daria 0 horas e a coluna
 * diria "corte pendente" enquanto o gateway ainda libera o cliente. Arredondar
 * para cima mantém a tabela e o comportamento contando a mesma coisa.
 */
export function graceRemaining(
  status: BillingStatus | null,
  pastDueSince: string | null
): string | null {
  if (status !== "past_due" || !pastDueSince) return null
  const remainingMs =
    new Date(pastDueSince).getTime() + BILLING_GRACE_HOURS * 3600_000 - Date.now()
  if (remainingMs <= 0) return "corte pendente"
  const hours = Math.ceil(remainingMs / 3600_000)
  if (hours < 24) return `corta em ${hours}h`
  return `corta em ${Math.ceil(hours / 24)}d`
}
