alter table templates
  add column if not exists plan text not null default 'VibeCoder';

alter table templates
  add constraint templates_plan_valid
  check (plan in ('VibeCoder', 'Pro', 'Max', 'Enterprise'));
