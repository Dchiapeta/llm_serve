-- Slots de stacks de uma máquina ("1 stack = 1 slot"): espelho SQL de
-- computeCapacity (lib/capacity.ts) com os defaults de machineStackCapacity
-- (lib/actions.ts) — footprint 16 GB e reserva KV 2 GB/usuário quando a
-- máquina não tem template. Função única usada pelo painel e pelo gateway
-- (via RPC), mesmo precedente da machine_lora_slots (0006), para a fórmula
-- não divergir entre TS e Python. Retorna 0 quando a capacidade é
-- desconhecida (sem VRAM) — o chamador decide o fallback.
create or replace function machine_stack_slots(p_machine_id uuid)
returns integer
language sql
security definer
as $$
  select case
    when m.vram_gb is null then coalesce(m.max_users, 0)
    when coalesce(t.kv_reserve_gb_per_user, 2) <= 0 then 0
    else least(
      floor(
        greatest(m.vram_gb - coalesce(t.model_footprint_gb, 16), 0)
        / coalesce(t.kv_reserve_gb_per_user, 2)
      )::integer,
      coalesce(m.max_users, 2147483647)
    )
  end
  from machines m
  left join templates t on t.id = m.template_id
  where m.id = p_machine_id
$$;
