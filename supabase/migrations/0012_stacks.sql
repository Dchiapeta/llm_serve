-- Stacks: cada stack é uma LLM/produto contratado por uma conta.
-- Uma conta pode ter várias stacks; o slug é o futuro subdomínio pelo qual
-- o cliente acessa o manager (roteamento por subdomínio ainda não implementado).
-- accounts.plan permanece intacto — o roteamento continua usando essa coluna.

create table if not exists stacks (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references accounts(id) on delete cascade,
  plan text not null default 'VibeCoder'
    check (plan in ('VibeCoder', 'Pro', 'Max', 'Enterprise')),
  purchase_date date not null default current_date,
  slug text not null unique,
  created_at timestamptz not null default now()
);

create index if not exists stacks_account_idx on stacks(account_id);

alter table stacks enable row level security;

-- Backfill: 1 stack por conta existente, com o plano atual da conta.
-- Slug determinístico a partir do md5(id): adjetivo-substantivo-NN;
-- row_number resolve colisões de base sem retry. Re-executável (not exists).
with base as (
  select
    a.id, a.plan, a.created_at,
    (array['brisk','calm','bold','swift','quiet','vivid','witty','sunny',
           'lucid','merry','noble','rapid','solid','tidy','zesty','agile'])
      [1 + (('x' || substr(md5(a.id::text), 1, 2))::bit(8)::int % 16)]
    || '-' ||
    (array['falcon','otter','maple','comet','harbor','lynx','ember','cedar',
           'delta','onyx','ridge','sable','tundra','vertex','willow','zephyr'])
      [1 + (('x' || substr(md5(a.id::text), 3, 2))::bit(8)::int % 16)]
    || '-' ||
    ((('x' || substr(md5(a.id::text), 5, 2))::bit(8)::int % 90) + 10)::text
      as base_slug
  from accounts a
  where not exists (select 1 from stacks s where s.account_id = a.id)
),
numbered as (
  select *, row_number() over (partition by base_slug order by created_at) as rn
  from base
)
insert into stacks (account_id, plan, purchase_date, slug, created_at)
select id, plan, created_at::date,
       case when rn = 1 then base_slug else base_slug || '-' || rn::text end,
       created_at
from numbered;
