// Camada de acesso ao estado de roteamento (tabela routing_state).
// Toda leitura/escrita do routing passa por aqui — nunca acessar a tabela
// direto de outros módulos. O equivalente Python para o gateway está em
// docker/gateway/routing.py.

import { createSupabaseAdmin } from "./supabase/server"
import type { RoutingHistory, RoutingState } from "./types"

async function recordRoutingHistory(
  entry: Pick<RoutingHistory, "account_id" | "event"> &
    Partial<Pick<RoutingHistory, "machine_id" | "from_machine_id" | "lora_adapter_id">>
): Promise<void> {
  const db = createSupabaseAdmin()
  const { error } = await db.from("routing_history").insert(entry)
  if (error) throw new Error(error.message)
}

export async function getClientLocation(
  stackId: string
): Promise<RoutingState | null> {
  // routing_state é escopado por STACK (PK stack_id) desde a migration 0029.
  const db = createSupabaseAdmin()
  const { data, error } = await db
    .from("routing_state")
    .select("*")
    .eq("stack_id", stackId)
    .maybeSingle<RoutingState>()
  if (error) throw new Error(error.message)
  return data
}

// Claim atômico via RPC: só um chamador vence a corrida e inicia o load
// (claimed = true); os demais recebem o estado atual com claimed = false.
// account_id vai junto só para popular a coluna denormalizada / histórico.
export async function claimClientLocation(
  stackId: string,
  accountId: string,
  machineId: string
): Promise<{ claimed: boolean; state: RoutingState }> {
  const db = createSupabaseAdmin()
  const { data, error } = await db.rpc("claim_route", {
    p_stack_id: stackId,
    p_account_id: accountId,
    p_machine_id: machineId,
  })
  if (error) throw new Error(error.message)
  const row = (data as (RoutingState & { claimed: boolean })[] | null)?.[0]
  if (!row) throw new Error("claim_route não retornou estado")
  const { claimed, ...state } = row
  if (claimed) {
    await recordRoutingHistory({
      account_id: accountId,
      event: "allocated",
      machine_id: state.machine_id,
      lora_adapter_id: state.lora_adapter_id,
    })
  }
  return { claimed, state }
}

export async function setClientLocation(
  stackId: string,
  patch: Partial<
    Pick<RoutingState, "machine_id" | "lora_adapter_id" | "lora_status">
  >
): Promise<void> {
  const db = createSupabaseAdmin()
  const { error } = await db
    .from("routing_state")
    .update({ ...patch, updated_at: new Date().toISOString() })
    .eq("stack_id", stackId)
  if (error) throw new Error(error.message)
}

// Libera o slot: sem adapter em VRAM e sem máquina — apto a novo claim.
export async function markSlotIdle(stackId: string): Promise<void> {
  const previous = await getClientLocation(stackId)
  await setClientLocation(stackId, {
    machine_id: null,
    lora_status: "unloaded",
  })
  if (previous?.machine_id) {
    await recordRoutingHistory({
      account_id: previous.account_id,
      event: "released",
      from_machine_id: previous.machine_id,
      lora_adapter_id: previous.lora_adapter_id,
    })
  }
}

export async function touchClientLocation(stackId: string): Promise<void> {
  const db = createSupabaseAdmin()
  const { error } = await db.rpc("touch_route", { p_stack_id: stackId })
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
