-- accounts.plan e accounts.system_prompt eram resíduo de antes de stacks
-- existir (0010_account_plan.sql). system_prompt já era só fallback morto
-- desde 0020; plan ainda era lido ao vivo pelo gateway pra rotear contas com
-- adapter LoRA (resolve_route em docker/gateway/main.py), ignorando de
-- propósito qual stack a chave pertencia — o bug real por trás desta
-- migration. O gateway (deploy anterior a esta migration) já passou a
-- resolver plano e adapter por stack_id via resolve_key_stack, então nada
-- mais lê essas duas colunas. Aplicar só depois de confirmar esse deploy no
-- ar — quebra qualquer processo antigo ainda rodando com o código velho.
alter table accounts drop column if exists plan;
alter table accounts drop column if exists system_prompt;
