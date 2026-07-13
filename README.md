# RunPod LLM Manager

Painel de gerenciamento de infraestrutura de LLM no RunPod: criação de
máquinas (pods) e templates, logs por máquina e por usuário, contas com chaves
de acesso HEX, slots por capacidade e dashboard de alocação.

## Arquitetura

- **Painel**: Next.js (App Router) + shadcn/ui (componentes ReUI) + Supabase
- **Máquinas**: pods do RunPod rodando a imagem [`docker/`](docker/README.md)
  (vLLM interno na porta 8001 + agent FastAPI na 8000)
- **Contas**: cada usuário recebe uma chave HEX; o agent valida a chave,
  repassa ao vLLM e mede uso por chave. O painel faz push das chaves ativas
  para o agent e coleta métricas/logs.

## Setup

### 1. Supabase

1. Crie um projeto em [supabase.com](https://supabase.com).
2. Rode a migration [supabase/migrations/0001_init.sql](supabase/migrations/0001_init.sql)
   no SQL Editor do projeto.
3. Em **Authentication → Users**, crie o usuário admin (e-mail + senha) que
   fará login no painel.

### 2. Variáveis de ambiente

```bash
cp .env.example .env.local
```

Preencha:

- `RUNPOD_API_KEY` — em [runpod.io → Settings → API Keys](https://www.runpod.io/console/user/settings)
- `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY`,
  `SUPABASE_SERVICE_ROLE_KEY` — em Project Settings → API do Supabase

### 3. Imagem Docker

Faça o build e push da imagem (ver [docker/README.md](docker/README.md)) e use
o nome dela (`seuusuario/vllm-agent:latest`) ao criar templates no painel.

### 4. Rodar

```bash
npm install
npm run dev
```

## Fluxo de uso

1. **Templates** → criar template (imagem, modelo, GPUs, parâmetros de capacidade)
2. **Máquinas** → nova máquina (template + GPU) — cria o pod no RunPod
3. **Contas & Chaves** → criar conta e gerar chave HEX (exibida uma única vez)
4. O usuário chama a LLM:

```bash
curl https://<pod-id>-8000.proxy.runpod.net/v1/chat/completions \
  -H "Authorization: Bearer <chave-hex>" \
  -H "Content-Type: application/json" \
  -d '{"model": "Qwen/Qwen2.5-7B-Instruct", "messages": [{"role": "user", "content": "Olá"}]}'
```

5. **Dashboard** → alocação de capacidade, distribuição de uso e atividade;
   detalhe da máquina mostra slots, uso por conta, logs e variáveis, com ações
   de desativar/iniciar/apagar.

## Adapters LoRA

Adapters LoRA por conta ficam no Supabase Storage, em um bucket privado
chamado `loras`, seguindo a convenção de path:

```
loras/{account_id}/{version}/adapter_config.json
loras/{account_id}/{version}/adapter_model.safetensors
```

O treino do adapter acontece fora deste sistema — o painel apenas registra
adapters já existentes no bucket (tabela `lora_adapters`, migration
[0004_lora_adapters.sql](supabase/migrations/0004_lora_adapters.sql)). O
registro valida que o prefixo contém arquivos antes de gravar. Formato
esperado: PEFT (`adapter_config.json` + `adapter_model.safetensors`).

Para subir um adapter use [scripts/upload-lora.mjs](scripts/upload-lora.mjs)
(o dashboard do Supabase cria pastas em vez de arquivos com facilidade).

> **⚠️ Risco conhecido — validação em GPU pendente.** O fluxo dinâmico de
> LoRA (Fase 2: load/unload em runtime no vLLM v0.24.0, medição de tempo de
> load via [scripts/test-lora-load.mjs](scripts/test-lora-load.mjs)) ainda
> **não foi validado contra um pod real com GPU** — bloqueado por limite de
> storage do Supabase (sem adapter real no bucket). As flags/endpoints foram
> confirmados no código-fonte da tag v0.24.0, mas a idempotência de
> load/unload e os tempos reais são comportamento assumido até o teste rodar.
> Quando o teste de GPU acontecer, validar em conjunto o **lifecycle inteiro
> da Fase 5** (idle reaper + migração ativa com
> [scripts/test-migration.py](scripts/test-migration.py)), não só a Fase 2
> isolada.

## Slots por capacidade

`slots_max = floor((VRAM da GPU − footprint do modelo) / reserva por usuário)`

Footprint e reserva são configurados por template ([lib/capacity.ts](lib/capacity.ts)).
