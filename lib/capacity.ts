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
  activeKeys: number
  maxUsers?: number | null
}

export type CapacityResult = {
  slotsMax: number
  slotsUsed: number
  slotsFree: number
  usagePct: number
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
  activeKeys,
  maxUsers,
}: CapacityInput): CapacityResult {
  const byVram = vramSlots({ vramGb, modelFootprintGb, kvReserveGbPerUser })
  const slotsMax =
    maxUsers != null
      ? vramGb != null
        ? Math.min(byVram, maxUsers)
        : maxUsers
      : byVram
  const slotsUsed = activeKeys
  const slotsFree = Math.max(slotsMax - slotsUsed, 0)
  const usagePct = slotsMax > 0 ? Math.round((slotsUsed / slotsMax) * 100) : 0
  return { slotsMax, slotsUsed, slotsFree, usagePct }
}
