-- Campos de Storage e Networking dos templates (espelham o RunPod).
alter table templates
  add column if not exists volume_gb integer not null default 0,
  add column if not exists volume_mount_path text not null default '/workspace',
  add column if not exists http_ports text[] not null default '{8000}',
  add column if not exists tcp_ports text[] not null default '{}';
