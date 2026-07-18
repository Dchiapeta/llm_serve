-- Chaves de API não expiravam nunca (só status active|revoked) — crítico
-- pra chave de onboarding/teste, que precisa se auto-encerrar sem depender
-- de revogação manual. NULL = nunca expira (chaves existentes seguem
-- válidas, sem nenhuma mudança de comportamento pra quem já tem chave).
alter table api_keys add column if not exists expires_at timestamptz;
create index if not exists api_keys_expires_idx on api_keys(expires_at) where expires_at is not null;
