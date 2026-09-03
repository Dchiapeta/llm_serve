// Cor do badge por plano de produto — mantém a mesma paleta usada em
// components/templates para o plano do template.
//
// Extraído de components/contas/contas-table.tsx pelo mesmo motivo de
// lib/billing-status.ts: passou a ter consumidor fora da árvore de /stacks
// (o dialog de info da conta em /contas), e importar de contas-table traria
// junto o bundle inteiro de row-actions e dialogs daquela página.

import type { TemplatePlan } from "./types"

export const PLAN_BADGE_VARIANT: Record<
  TemplatePlan,
  "secondary" | "info-light" | "success-light" | "warning-light" | "primary-light"
> = {
  Go: "secondary",
  Pro: "info-light",
  Max: "success-light",
  Enterprise: "warning-light",
  // Variante distinta das quatro da escada de LLM de propósito: "Image" é
  // outra linha de produto, e confundi-la visualmente com um degrau de plano
  // levaria a suporte errado.
  Image: "primary-light",
}
