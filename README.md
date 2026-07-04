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

## Slots por capacidade

`slots_max = floor((VRAM da GPU − footprint do modelo) / reserva por usuário)`

Footprint e reserva são configurados por template ([lib/capacity.ts](lib/capacity.ts)).
