# Pod de geração de imagem — FLUX.2 Klein 4B

Imagem: `dchiapeta/diffusers-agent:flux2-klein-4b-0.1.1`

Ocupa o mesmo lugar arquitetural do pod de vLLM, trocando só o processo de
inferência:

```
cliente → gateway → agent do pod (:8000) → server.py diffusers (:8001)
```

O `agent` é o **mesmo binário** de `docker/agent/` que roda nos pods de LLM — ele
autentica a chave, conta uso e expõe `/admin/*`. Nada nele é específico de vLLM
depois das duas mudanças descritas em "Contrato com o agent" abaixo.

## Arquivos

| Arquivo | Papel |
|---|---|
| `Dockerfile` | base PyTorch pinada por digest + lock + agent + server |
| `requirements.in` | dependências **declaradas** (intenção) |
| `requirements.lock` | dependências **resolvidas** — é o que entra na imagem |
| `lock-deps.sh` | gera o lock contra a mesma base do Dockerfile |
| `entrypoint.sh` | sobe server.py na 8001 e o agent na 8000 |
| `server.py` | FastAPI + diffusers (torch, PIL, rotas) |
| `policy.py` | validação e fila — **puro**, sem torch |
| `test_policy.py` | testes do módulo puro (sem GPU, sem fastapi) |
| `test_server_routes.py` | testes do wiring HTTP (stubs de torch/diffusers) |

## Build

Tudo é congelado **antes** do primeiro boot: se o digest e o lock fossem fixados
depois de testar, o segundo build já seria outra imagem e o teste não valeria
para ela.

```bash
# 1. só quando requirements.in mudar (gera requirements.lock)
cd docker/image && ./lock-deps.sh

# 2. build + push, com contexto em docker/
cd docker
docker buildx build -f image/Dockerfile --platform linux/amd64 \
  -t dchiapeta/diffusers-agent:flux2-klein-4b-0.1.1 --push .

# 3. registrar o digest produzido, abaixo
docker buildx imagetools inspect dchiapeta/diffusers-agent:flux2-klein-4b-0.1.1
```

**A tag nunca é re-pushada.** Mudança de conteúdo é a versão seguinte. É isso
que torna `templates.image` apontando para a tag equivalente a apontar para um
digest — e é a lição do template que ficou pinado em `:v2` e serviu meses uma
imagem pré-tool-calling.

### Versões

| Tag | Estado |
|---|---|
| `flux2-klein-4b-0.1.0` | **NÃO USAR.** Publicada antes da revisão. Devolve 500 (em vez de 400) para qualquer campo escalar com tipo errado; a admissão da fila é furável por handler cancelado (`CancelledError` não tratado — medido: `capacity=2` admitindo 21 gerações); `stop()` no meio de uma geração pendura a request para sempre; worker morto responde 504 indefinidamente com `/health` em 200; sem degradação pós-boot; sem teto de multipart no parser; recusa o `model` que o `pin_model` do gateway fixaria. |
| `flux2-klein-4b-0.1.1` | Atual. Todas as correções acima, cada uma com teste. |

A `0.1.0` fica no registry de propósito, e não é deletada: apagá-la faria a
referência a ela em qualquer log ou anotação antiga virar um mistério, em vez de
uma versão conhecidamente ruim.

### Base

```
pytorch/pytorch:2.13.0-cuda12.6-cudnn9-runtime
@sha256:6acf597eeb8e376a96580dde4952f37cc017fef732bb40bfc73f28f25e3f64b4
```

Digest verificado no Docker Hub e confirmado no pull (03/09/2026). Single-arch
`linux/amd64`, que é a plataforma do build. A `2.14.0-cuda12.6-cudnn9-runtime`
existe, mas foi publicada em 02/09/2026 — a 2.13.0 (08/07/2026) é a madura.

Duas particularidades da base, descobertas construindo:

- **Sem conda.** Python 3.12.3 do sistema, `torch 2.13.0+cu126` instalado com pip
  em `/usr/local/lib/python3.12/dist-packages`.
- **PEP 668.** Existe `/usr/lib/python3.12/EXTERNALLY-MANAGED`, então **todo**
  `pip install` precisa de `--break-system-packages`. Não é gambiarra: os
  pacotes vão para o mesmo `/usr/local` onde a própria base pôs o torch, e nada
  gerenciado pelo apt é tocado.

### Digest da imagem produzida

`flux2-klein-4b-0.1.1`, publicada em 03/09/2026:

```
índice OCI   sha256:66c8f155356b5168ee165d780e19a3ab2fefe2618052819334dfdd9bbe283c5f
linux/amd64  sha256:8a135f6e6fb0a6ce799460cb3e21d9f15e85cd0b5fb00ced48bdac8ca097bda5
```

O `templates.image` aponta para a **tag**, não para o digest — a imutabilidade
por convenção (a tag nunca é re-pushada) é o que torna as duas coisas
equivalentes. O digest fica registrado aqui para auditoria: é como se confere
que o pod que rodou o load test é o mesmo artefato que a tag ainda serve.

Para verificar que uma tag publicada tem o que se espera, sem subir GPU:

```bash
docker run --rm --platform linux/amd64 --entrypoint bash \
  dchiapeta/diffusers-agent:flux2-klein-4b-0.1.1 -c \
  'cd /opt/agent && grep -l WorkerStopped policy.py && grep -l MODEL_ALIASES server.py'
```

### Por que o lock é um delta

`requirements.lock` contém só o que **falta** na base, não um `pip freeze`
completo. Um freeze completo traria `torch`, `torchvision` e as ~60 libs que já
vêm na imagem, e o `pip install --no-deps` do Dockerfile reinstalaria o torch de
PyPI — trocando a build de CUDA da base por uma wheel genérica de 2,5 GB que,
no pior caso, não enxerga a GPU. O `lock-deps.sh` tem uma guarda que falha o
build se `torch`/`nvidia-*` aparecerem no lock.

Resolução atual: 29 pacotes, com `diffusers==0.40.0` e `transformers==5.16.1`
(compatibilidade do par verificada dentro da imagem: `Flux2KleinPipeline` e
`Qwen3ForCausalLM` importam).

O Dockerfile tem um `RUN python3 -c "import …"` que exercita a cadeia inteira em
tempo de **build**. Sem ele, `--no-deps` transforma qualquer suposição errada
sobre o que a base traz num `ImportError` no **boot** — e o boot acontece numa
A40 alugada, depois de baixar 16 GB de pesos. Verificado que `pillow` vem da
base via `torchvision`, e que `anyio` 4.15 **não** declara mais `sniffio` como
dependência (a ausência dele no lock está correta; os caminhos que o usariam —
`anyio.to_thread.run_sync`, `starlette.run_in_threadpool`, handler síncrono no
FastAPI — foram testados dentro da imagem).

## Contrato com o agent

O agent é compartilhado, e só duas coisas nele precisaram mudar:

1. **`SERVER_PROCESS_MATCH`** (`docker/agent/main.py:_vllm_process_alive`) — a
   agulha procurada em `/proc/*/cmdline` para distinguir "carregando" de
   "morreu". O default é a linha de comando do vLLM; o `entrypoint.sh` daqui
   exporta `/opt/agent/server.py`. Sem isso, um pod de imagem saudável
   reportaria `vllm_alive=false` durante todo o boot e o painel mostraria
   **"Falha"** enquanto ele só baixava 16 GB de pesos.
2. **`Content-Type` de multipart repassado e normalizado** no proxy — era fixo
   em `application/json`. O `/v1/images/edits` é multipart e o `boundary=...`
   viaja nesse header: sobrescrevê-lo torna o corpo impossível de parsear. Duas
   sutilezas, as duas medidas no container:
   - fora do multipart o header continua **forçado** em `application/json`.
     Repassar sempre seria regressão: cliente que manda corpo JSON declarando
     `text/plain` funciona hoje porque o agent corrige o header.
   - o media type é minusculizado, **preservando os parâmetros**. Media type é
     case-insensitive (RFC 9110 §8.3.1), mas o `request.form()` do Starlette
     compara com o literal `b"multipart/form-data"` sem normalizar: um
     `Multipart/Form-Data` chega com o header intacto e ainda assim cai no ramo
     de form **vazio**. O `boundary` não pode ser normalizado — ele é
     case-sensitive.

Os nomes de campo `vllm_ready`/`vllm_alive` do `/health` **não** mudam: são o
contrato lido por `lib/machines.ts`, `docker/gateway/main.py` e `lifecycle.py`.
Mesma coisa com `VLLM_URL` e `VLLM_LOG_FILE` — manter os nomes é o que permite
reusar o agent sem tocar nele.

## API

| Rota | Content-Type | Uso |
|---|---|---|
| `GET /health` | — | 200 só depois do warmup; 503 durante o load, com o erro |
| `GET /metrics` | texto | consumido pelo `/admin/vllm-metrics` do agent |
| `GET /v1/models` | — | devolve `IMAGE_SERVED_MODEL_NAME` |
| `POST /v1/images/generations` | `application/json` | text-to-image |
| `POST /v1/images/edits` | `multipart/form-data` | image-to-image, até 4 refs |

`edits` aceita o campo como `image` **e** como `image[]` — os exemplos oficiais
da OpenAI usam `image[]` para múltiplas imagens, e `image[]` não é identificador
Python válido, então o form é lido à mão em vez de declarado como parâmetro.

Extensões nossas nas duas rotas: `steps`, `guidance_scale`, `seed`.

Recusas explícitas, todas com `error.code` estável:

| Situação | Status | `code` |
|---|---|---|
| `mask` presente | 400 | `mask_not_supported` |
| `response_format: "url"` | 400 | `response_format_not_supported` |
| `size` fora da allowlist | 400 | `invalid_size` |
| `model` diferente do servido | 404 | `model_not_found` |
| referência não-imagem (magic bytes) | 400 | `unsupported_image_format` |
| referência acima do teto | 413 | `image_too_large` |
| mais referências que o teto | 400 | `too_many_reference_images` |
| `image` no corpo JSON do `generations` | 400 | `wrong_route_for_reference_image` |
| campo escalar com tipo errado (`size: 1024`, `steps: "abc"`, campo enviado como arquivo no multipart) | 400 | `invalid_size`, `invalid_steps`, … |
| multipart inválido ou com excesso de arquivos/campos | 400 | `invalid_multipart` |
| fila cheia | 429 | `queue_full` |
| espera na fila excedida | 504 | `queue_timeout` |
| modelo carregando, em falha, degradado, ou worker parado | 503 | `model_not_ready` |
| worker interrompido com a geração em voo (shutdown) | 503 | `worker_stopped` |
| falha real de geração (OOM, erro de CUDA) | 500 | `generation_failed` |

O `Retry-After: 5` do 429 só chega a quem bate **direto no pod**: o agent monta
uma `JSONResponse` nova e não repassa headers do upstream. Fica porque é grátis
e correto; pelo gateway o cliente se orienta pelo status.

Tipos: todo campo escalar aceita o valor nativo do JSON **ou** string numérica
(multipart entrega tudo como texto). O que é recusado com 400 é o resto —
incluindo `true` onde se espera inteiro (`isinstance(True, int)` é `True` em
Python) e float truncante (`steps: 7.9`, porque `int(7.9)` geraria com 7 sem o
cliente saber).

`mask` merece uma nota: o `Flux2KleinInpaintPipeline` **existe** no diffusers
0.40 e aceita `mask_image`. O 400 não é "o modelo não suporta" — é que este pod
carrega só o `Flux2KleinPipeline`, e uma segunda classe de pipeline está fora
desta versão. Aceitar e ignorar seria pior que recusar: o cliente receberia a
imagem editada inteira achando que só a região da máscara mudou.

## Quando o pod se declara inapto

`GET /health` responde 503 em **quatro** situações, e as quatro precisam virar
503 porque o agent traduz o status code em `vllm_ready` — que é o que o
reconciliador usa para manter a máquina em "running" e o reaper para contá-la
como em uso. Qualquer uma delas respondendo 200 produz o pior estado possível:
máquina saudável no painel, erro em toda geração.

| Situação | `error` do /health |
|---|---|
| carregando pesos / warmup | `null` (com `loading: true`) |
| falha no boot | `falha no boot: …` |
| degradado depois do boot | `degradado: N falhas consecutivas; …` |
| task consumidora morta | `worker de geração parado: …` |

**Degradação pós-boot** existe porque `READY=True` só diz que o load e o warmup
passaram. Um `CUDA illegal memory access` depois disso deixa o pipeline
inutilizável com o processo **vivo** — nos pods de vLLM esse cenário mata o
processo e o `vllm_alive=false` denuncia, aqui não. Contam apenas falhas
**consecutivas** e **não-de-cliente** (`IMAGE_DEGRADED_AFTER_FAILURES`, default
3): uma imagem corrompida não é sintoma de GPU quebrada, e uma falha isolada
pode ser transitória.

## Fila: um consumidor, e por quê

O caminho óbvio — `Semaphore(1)` + `asyncio.wait_for` em volta do `to_thread` —
está errado de um jeito que só aparece sob carga: `wait_for` cancela a **espera**,
não a thread. Num timeout ele soltaria o semáforo com a geração anterior ainda
na GPU, a próxima entraria, e o pipeline seria usado por duas threads ao mesmo
tempo.

Aqui a serialização não depende de timeout: existe **uma** task consumidora, e
ela roda uma geração por vez. O timeout só corta espera de fila.

Três detalhes que os testes fixam (`test_policy.py`):

- **`started` event** — separa "ainda dá para desistir" de "já está na GPU". No
  timeout há um re-check de `started.is_set()`, senão uma geração em curso seria
  marcada como cancelada.
- **`asyncio.shield`** no resultado — sem ele, um cliente que desconecta cancela
  o `Future`, o `set_result` do worker levanta `InvalidStateError` e a task
  consumidora morre: o pod para de gerar para **todos** os clientes seguintes.
- **`future.done()` antes de todo `set_*`** — a outra metade da mesma proteção.
- **`CancelledError` tratado junto com `TimeoutError`** — `wait_for` levanta
  `CancelledError`, não `TimeoutError`, quando é a task do handler que é
  cancelada. Tratando só o timeout, a vaga de `_in_flight` era liberada e o job
  ficava na fila **sem** `cancelled=True`: o worker gerava a imagem para um
  cliente que não existe mais, e a admissão era furável (medido: `capacity=2`
  admitindo 21 gerações). O job cancelado também solta o `payload` na hora, em
  vez de segurar até 60 MB de referências até o worker chegar nele.
- **Worker cancelado em voo resolve o Future com `WorkerStopped`** — o `shield`
  protege o Future de um cancelamento do handler e, por isso mesmo, impede que o
  awaiter perceba a morte do **worker**. Sem isso, um `stop()` no meio de uma
  geração (shutdown do uvicorn) deixava a request pendurada para sempre.
- **`start()` substitui uma task morta** (`_worker is None or done()`) e
  `worker_error` registra a causa — antes, worker morto significava 504 em toda
  request indefinidamente com o `/health` em 200.

`IMAGE_QUEUE_CAPACITY` é **total em voo** (em execução + esperando), não tamanho
de fila. Com `maxsize` no `asyncio.Queue` a fronteira do 429 oscilava entre
`capacity` e `capacity+1` conforme o worker tivesse sido escalonado entre os
`put_nowait` — um cliente veria o 429 num ponto diferente a cada burst.

## Env

Injetadas **por máquina** pelo painel (`lib/actions.ts:podInputFromTemplate`),
não ficam no template: `MODEL_NAME`, `AGENT_ADMIN_SECRET`, `GPU_COUNT`,
`MAX_USERS`, `HF_TOKEN`.

Do `env` do template (ver `scripts/_tmp-create-image-template.mjs`):

| Var | Default | Nota |
|---|---|---|
| `HF_HOME` | — | **essencial**: aponta para o volume. Sem isso os pesos vão para o container disk, que o RunPod reconstrói a cada start — 16 GB rebaixados a cada despausa, não só na recriação |
| `IMAGE_MODEL_REVISION` | `main` | commit do HF; sem ele o pod deixa de ser reproduzível |
| `IMAGE_SERVED_MODEL_NAME` | `flux2-klein-4b` | alias validado no campo `model` |
| `IMAGE_DTYPE` | `bfloat16` | |
| `IMAGE_STEPS` / `IMAGE_STEPS_MAX` | `4` / `8` | o checkpoint é distilled (`is_distilled: true`) |
| `IMAGE_GUIDANCE_SCALE` | `1.0` | `guidance_embeds: false` no transformer |
| `IMAGE_MAX_SEQUENCE_LENGTH` | `512` | |
| `IMAGE_DEFAULT_SIZE` / `IMAGE_ALLOWED_SIZES` | `1024x1024` / 3 resoluções | allowlist fechada: resolução é custo de VRAM e de tempo |
| `IMAGE_IMAGES_PER_REQUEST_MAX` | `1` | teto do `n` |
| `IMAGE_OUTPUT_FORMAT` | `png` | |
| `IMAGE_MAX_REFERENCE_IMAGES` | `4` | |
| `IMAGE_ALLOWED_FORMATS` | `png,jpeg,webp` | detectado por magic bytes, não por content-type |
| `IMAGE_MAX_FILE_SIZE_MB` | `15` | |
| `IMAGE_QUEUE_CAPACITY` | `4` | total em voo |
| `IMAGE_QUEUE_WAIT_TIMEOUT_S` | `60` | e não 120: o edge do RunPod corta em ~100-127 s, então 120 nunca dispararia — o cliente receberia 524 do Cloudflare em vez do nosso 504 |
| `IMAGE_WARMUP_RUNS` | `2` | `/health` só fica 200 depois deles |
| `IMAGE_ALLOW_TF32` | `true` | A40 é SM 8.6 |
| `PYTORCH_ALLOC_CONF` | — | nome atual; `PYTORCH_CUDA_ALLOC_CONF` é só alias de compatibilidade |
| `IMAGE_DEGRADED_AFTER_FAILURES` | `3` | falhas de geração consecutivas até o /health virar 503 |
| `SERVER_PROCESS_MATCH` | `/opt/agent/server.py` | tem que casar com a linha de comando do entrypoint |

**Não existe `IMAGE_WORKER_CONCURRENCY`.** A serialização é estrutural (uma
única task consumidora), não configurável. Uma env com esse nome seria pior que
nenhuma: pareceria o botão de paralelismo e não faria nada.

## Testes

```bash
# módulo puro — não precisa de nada além de pytest
python3 -m pytest docker/image/test_policy.py docker/agent/test_proxy_policy.py -q

# wiring HTTP — precisa de fastapi + python-multipart (venv)
<venv>/bin/python -m pytest docker/image/test_server_routes.py -q
```

Smoke test de integração sem GPU, que é o que valida as duas mudanças no agent:

```bash
cd docker && docker buildx build -f image/Dockerfile --platform linux/amd64 \
  --load -t diffusers-agent:local-test .

docker run -d --name img-smoke --platform linux/amd64 -p 18000:8000 \
  -e MODEL_NAME=trystac-nao-existe/x -e AGENT_ADMIN_SECRET=s \
  diffusers-agent:local-test

# ready=false + alive=true prova o SERVER_PROCESS_MATCH
curl -s http://127.0.0.1:18000/health

# 400 unsupported_image_format (e não erro de form) prova o Content-Type
curl -X POST http://127.0.0.1:18000/v1/images/edits \
  -H "Authorization: Bearer <chave injetada via /admin/upsert-keys>" \
  -F "prompt=x" -F "image[]=@nao-e-imagem.png"
```

## Limitações conhecidas

- **O gateway ainda não expõe estas rotas.** `ALLOWED_V1` de
  `docker/gateway/main.py` não tem `images/*`, então elas são alcançáveis só pela
  URL pública do pod. E o `MAX_BODY_BYTES` de 8 MB é aplicado no catch-all
  `/v1/{path}`: uma referência de 15 MB vira ~20 MB e leva 413. Antes de expor
  comercialmente é preciso um limite próprio para as rotas de imagem e um
  caminho que não copie o corpo inteiro em memória várias vezes.
- **Sem moderação e sem storage.** A resposta é `b64_json` direto, nada é
  persistido, e não há classificação de conteúdo de prompt nem de imagem.
- **Uso não é contabilizado.** Geração de imagem não produz tokens, então
  `gateway_requests.tokens_in/out` e `usage_metrics` ficam zerados — e
  `check_token_quota` fica cega para este workload.
- **Volume não sobrevive à recriação do pod.** `CreatePodInput`
  (`lib/runpod.ts`) não expõe Network Volume, então `recreateMachine` rebaixa os
  16 GB. Stop/start (auto-pausa) preserva.
