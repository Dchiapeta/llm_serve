// Cálculo de slots por capacidade.
// Capacidade teórica: quanto da VRAM sobra para KV-cache depois do modelo
// carregado, dividido pela reserva por usuário definida no template.
// O teto manual (max_users) nunca amplia a capacidade física: o slotsMax
// efetivo é min(teto, capacidade por VRAM), exceto quando a VRAM é
// desconhecida — aí o teto manual vira a única fonte de verdade.

export type CapacityInput = {
  vramGb: number | null
  modelFootprintGb: number
  kvReserveGbPerUser: number
  // Slots ocupados. Desde a migration 0032 a ocupação de stacks é PONDERADA
  // pela classe de uso (machine_stack_load: low=1.0, medium=1.5, high=3.0),
  // então pode ser fracionária — comparações de vaga devem usar >= 1 (peso
  // mínimo de um entrante), não > 0.
  occupied: number
  maxUsers?: number | null
}

export type CapacityResult = {
  slotsMax: number
  slotsUsed: number
  slotsFree: number
  usagePct: number
}

// Peso de ocupação por classe de uso — espelho dos DEFAULTS do SQL
// (usage_class_weight, migration 0032) e do gateway (usage_class.py).
// A fonte da verdade para ALOCAÇÃO é o SQL (machine_stack_load, que aplica
// override do template); este espelho serve ao display server-side, que
// soma pesos em memória para várias máquinas de uma vez.
export type UsageClass = "low" | "medium" | "high"
export const USAGE_CLASS_WEIGHTS: Record<UsageClass, number> = {
  low: 1,
  medium: 1.5,
  high: 3,
}

export function stackWeight(usageClass: string | null | undefined): number {
  return USAGE_CLASS_WEIGHTS[usageClass as UsageClass] ?? USAGE_CLASS_WEIGHTS.low
}

export function vramSlots({
  vramGb,
  modelFootprintGb,
  kvReserveGbPerUser,
}: Pick<
  CapacityInput,
  "vramGb" | "modelFootprintGb" | "kvReserveGbPerUser"
>): number {
  const usable = Math.max((vramGb ?? 0) - modelFootprintGb, 0)
  return kvReserveGbPerUser > 0 ? Math.floor(usable / kvReserveGbPerUser) : 0
}

export function computeCapacity({
  vramGb,
  modelFootprintGb,
  kvReserveGbPerUser,
  occupied,
  maxUsers,
}: CapacityInput): CapacityResult {
  const byVram = vramSlots({ vramGb, modelFootprintGb, kvReserveGbPerUser })
  const slotsMax =
    maxUsers != null
      ? vramGb != null
        ? Math.min(byVram, maxUsers)
        : maxUsers
      : byVram
  const slotsUsed = occupied
  const slotsFree = Math.max(slotsMax - slotsUsed, 0)
  const usagePct = slotsMax > 0 ? Math.round((slotsUsed / slotsMax) * 100) : 0
  return { slotsMax, slotsUsed, slotsFree, usagePct }
}

// ---------- Capacidade multi-LoRA ----------
// Com 1 modelo base carregado, cada cliente ativo custa o footprint do seu
// adapter LoRA + a reserva de KV-cache. O footprint por adapter depende do
// rank — vem do template (lora_footprint_gb), medido na prática com
// scripts/test-lora-load.mjs + nvidia-smi (diff de VRAM antes/depois do load).
// A MESMA fórmula existe no banco como machine_lora_slots() (migration 0006),
// usada pelo gateway na alocação — manter as duas em sincronia.

export type LoraCapacityInput = {
  vramGb: number | null
  baseModelFootprintGb: number
  loraFootprintGb: number
  kvReserveGbPerUser: number
  // rotas em loading | loaded | migrating na máquina (routing_state)
  activeAdapters: number
  // teto do --max-loras do pod (MAX_LORAS); null = sem teto conhecido
  maxLoras?: number | null
}

export function loraSlots({
  vramGb,
  baseModelFootprintGb,
  loraFootprintGb,
  kvReserveGbPerUser,
  maxLoras,
}: Omit<LoraCapacityInput, "activeAdapters">): number {
  const perAdapter = loraFootprintGb + kvReserveGbPerUser
  if (perAdapter <= 0) return 0
  const usable = Math.max((vramGb ?? 0) - baseModelFootprintGb, 0)
  const byVram = Math.floor(usable / perAdapter)
  return maxLoras != null ? Math.min(byVram, maxLoras) : byVram
}

export function computeLoraCapacity(input: LoraCapacityInput): CapacityResult {
  const slotsMax = loraSlots(input)
  const slotsUsed = input.activeAdapters
  const slotsFree = Math.max(slotsMax - slotsUsed, 0)
  const usagePct = slotsMax > 0 ? Math.round((slotsUsed / slotsMax) * 100) : 0
  return { slotsMax, slotsUsed, slotsFree, usagePct }
}
