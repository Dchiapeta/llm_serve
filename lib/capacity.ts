// Cálculo de slots por capacidade.
// Capacidade teórica: quanto da VRAM sobra para KV-cache depois do modelo
// carregado, dividido pela reserva por usuário definida no template.

export type CapacityInput = {
  vramGb: number | null
  modelFootprintGb: number
  kvReserveGbPerUser: number
  activeKeys: number
}

export type CapacityResult = {
  slotsMax: number
  slotsUsed: number
  slotsFree: number
  usagePct: number
}

export function computeCapacity({
  vramGb,
  modelFootprintGb,
  kvReserveGbPerUser,
  activeKeys,
}: CapacityInput): CapacityResult {
  const usable = Math.max((vramGb ?? 0) - modelFootprintGb, 0)
  const slotsMax =
    kvReserveGbPerUser > 0 ? Math.floor(usable / kvReserveGbPerUser) : 0
  const slotsUsed = activeKeys
  const slotsFree = Math.max(slotsMax - slotsUsed, 0)
  const usagePct = slotsMax > 0 ? Math.round((slotsUsed / slotsMax) * 100) : 0
  return { slotsMax, slotsUsed, slotsFree, usagePct }
}
