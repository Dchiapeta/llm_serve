// Camada de acesso ao estado de roteamento (tabela routing_state).
// Toda leitura/escrita do routing passa por aqui — nunca acessar a tabela
// direto de outros módulos. O equivalente Python para o gateway está em
// docker/gateway/routing.py.

import { createSupabaseAdmin } from "./supabase/server"
import type { RoutingState } from "./types"

export async function getClientLocation(
  accountId: string
): Promise<RoutingState | null> {
  const db = createSupabaseAdmin()
  const { data, error } = await db
    .from("routing_state")
    .select("*")
    .eq("account_id", accountId)
    .maybeSingle<RoutingState>()
  if (error) throw new Error(error.message)
  return data
}

// Claim atômico via RPC: só um chamador vence a corrida e inicia o load
// (claimed = true); os demais recebem o estado atual com claimed = false.
export async function claimClientLocation(
  accountId: string,
  machineId: string
): Promise<{ claimed: boolean; state: RoutingState }> {
  const db = createSupabaseAdmin()
  const { data, error } = await db.rpc("claim_route", {
    p_account_id: accountId,
    p_machine_id: machineId,
  })
  if (error) throw new Error(error.message)
  const row = (data as (RoutingState & { claimed: boolean })[] | null)?.[0]
  if (!row) throw new Error("claim_route não retornou estado")
  const { claimed, ...state } = row
  return { claimed, state }
}

export async function setClientLocation(
  accountId: string,
  patch: Partial<
    Pick<RoutingState, "machine_id" | "lora_adapter_id" | "lora_status">
  >
): Promise<void> {
  const db = createSupabaseAdmin()
  const { error } = await db
    .from("routing_state")
    .update({ ...patch, updated_at: new Date().toISOString() })
    .eq("account_id", accountId)
  if (error) throw new Error(error.message)
}

// Libera o slot: sem adapter em VRAM e sem máquina — apto a novo claim.
export async function markSlotIdle(accountId: string): Promise<void> {
  await setClientLocation(accountId, {
    machine_id: null,
    lora_status: "unloaded",
  })
}

export async function touchClientLocation(accountId: string): Promise<void> {
  const db = createSupabaseAdmin()
  const { error } = await db.rpc("touch_route", { p_account_id: accountId })
  if (error) throw new Error(error.message)
}

export async function listRoutesByMachine(
  machineId: string
): Promise<RoutingState[]> {
  const db = createSupabaseAdmin()
  const { data, error } = await db
    .from("routing_state")
    .select("*")
    .eq("machine_id", machineId)
  if (error) throw new Error(error.message)
  return (data ?? []) as RoutingState[]
}
