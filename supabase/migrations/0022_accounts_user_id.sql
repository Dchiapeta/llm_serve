-- O signup do painel (supabase.auth.signUp) nunca criava a linha
-- correspondente em accounts: usuário ficava só em auth.users e sumia da
-- listagem de contas. Liga account <-> usuário de auth por FK em vez de só
-- casar por e-mail (frágil: case, corrida, e-mail trocado depois).
alter table accounts
  add column if not exists user_id uuid unique references auth.users(id) on delete set null;
