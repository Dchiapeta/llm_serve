-- Relógio de atividade por STACK + liberação de slot base por ociosidade.
--
-- Motivação: o idle reaper (gateway) só libera slot no path LoRA — descarrega
-- o adapter após IDLE_RELEASE_MINUTES e zera routing_state.machine_id, e a
-- próxima request re-aloca em qualquer máquina do plano com vaga. Os planos
-- rodam MODELO BASE por stack, cuja "casa" é stacks.machine_id, e esse vínculo
-- nunca era liberado por ociosidade (só quando a máquina morria ou via painel).
-- Resultado: um usuário ocioso ocupava vaga (ponderada por classe de uso —
-- machine_stack_load, 0032) indefinidamente, bloqueando capacidade.
--
-- Esta migration dá à stack um relógio próprio de atividade (machines já tinha
-- o seu — 0015). O gateway passa a: (1) tocar stacks.last_activity_at em toda
-- request; (2) após IDLE_RELEASE_MINUTES sem atividade, zerar stacks.machine_id
-- (libera a vaga base); (3) na volta, re-alocar numa máquina running do mesmo
-- plano com vaga ponderada. Nenhuma migração forçada — só afeta a "casa" da
-- stack quando ela mesma fica ociosa.

alter table stacks add column if not exists last_activity_at timestamptz not null default now();

-- Índice parcial pra query do reaper (só stacks com casa alocada interessam).
create index if not exists stacks_idle_idx on stacks (last_activity_at) where machine_id is not null;
