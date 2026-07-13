alter table templates
  add column if not exists gpu_count integer not null default 1;

alter table templates
  add constraint templates_gpu_count_positive check (gpu_count > 0);
