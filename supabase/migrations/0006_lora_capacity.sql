-- Capacidade multi-LoRA: quantos adapters cabem em VRAM dado 1 modelo base
-- já carregado. O footprint por adapter depende do rank — é configurável no
-- template (medir com scripts/test-lora-load.mjs + nvidia-smi), não hardcoded.
alter table templates
  add column if not exists lora_footprint_gb numeric not null default 0.5;

-- Slots LoRA de uma máquina: floor((vram − modelo base) / (adapter + reserva KV)).
-- Função única usada pelo painel (via view/consulta) e pelo gateway (via RPC),
-- para a fórmula não divergir entre TS e Python. Retorna null quando a
-- capacidade é desconhecida (sem VRAM ou sem template) — o chamador decide o
-- fallback.
create or replace function machine_lora_slots(p_machine_id uuid)
returns integer
language sql
security definer
as $$
  select case
    when m.vram_gb is null or t.id is null then null
    when (t.lora_footprint_gb + t.kv_reserve_gb_per_user) <= 0 then 0
    else floor(
      greatest(m.vram_gb - t.model_footprint_gb, 0)
      / (t.lora_footprint_gb + t.kv_reserve_gb_per_user)
    )::integer
  end
  from machines m
  left join templates t on t.id = m.template_id
  where m.id = p_machine_id
$$;
