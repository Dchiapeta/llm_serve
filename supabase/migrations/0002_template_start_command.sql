-- Comando de inicialização do container (Container start command do RunPod)
-- Guardado como texto multilinha; convertido em argv na hora de chamar o RunPod.
alter table templates
  add column if not exists start_command text;
