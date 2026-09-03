# API Reference

Referência rápida das requests para cada caso de uso do serviço. Para explicações
detalhadas (retry, streaming, limites, exemplos em várias linguagens), veja
[integracao.md](integracao.md) — este documento é só o "o que mandar" de cada rota.

Em todos os casos:

| | |
|---|---|
| Base URL | `https://api.trystac.com` |
| Autenticação | header `Authorization: Bearer <SUA_CHAVE_DE_ACESSO>` |
| Content-Type | `application/json`, exceto `/v1/documents/extract`, `/v1/images/extract` e `/v1/images/edits` (`multipart/form-data`) |

---

## 1. Mensagem (chat)

Conversa de texto simples, formato compatível com a OpenAI.

```
POST /v1/chat/completions
```

```json
{
  "model": "go-base",
  "max_tokens": 8000,
  "messages": [
    { "role": "user", "content": "oi" }
  ]
}
```

```bash
curl https://api.trystac.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $STACK_API_KEY" \
  -d '{
    "messages": [{"role": "user", "content": "oi"}]
  }'
```

| Campo | Obrigatório | Descrição |
|---|---|---|
| `messages` | sim | Lista de mensagens (`role`: `system`, `user`, `assistant` ou `tool`) |
| `model` | não | Ignorado — o serviço sempre usa o modelo do seu plano |
| `max_tokens` | não | Piso de 8000, teto de 16000 |
| `stream` | não | `true` para receber a resposta em SSE, token a token |

**Resposta:** texto em `choices[0].message.content`.

Formato Anthropic equivalente: `POST /v1/messages` com header `x-api-key` em vez de
`Authorization` (veja [integracao.md](integracao.md#a-request)). Também existem
`/v1/completions`, `/v1/embeddings`, `/v1/responses` e `GET /v1/models`.

`/v1/messages` e `/v1/responses` são as rotas do Claude Code e do Codex, e por isso
exigem plano **Pro ou superior** — no Go elas respondem `403`. `/v1/chat/completions`
funciona em todos os planos.

**System prompt: da chave ou da stack.** Sem mensagem `system` na request, o serviço
injeta a instrução configurada — a **da própria chave**, se você ligou "Prompt" no
painel para ela, ou a da stack (Comportamento) caso contrário. É o que permite usar a
mesma stack para coisas diferentes sem mudar o código: cada integração fica com a sua
chave, e a request continua sendo só `model` + `messages`. Mandar `system` na request
sobrescreve os dois — é por isso que Claude Code, Codex e Cursor, que embutem o próprio
system prompt, não são afetados. A base de conhecimento (RAG) é sempre a da stack,
independente da opção escolhida na chave.

---

## 2. Imagem — OCR / leitura de imagem

Não é um endpoint separado: é a **mesma rota de chat**, enviando a imagem como uma
parte `image_url` dentro do `content` da mensagem, junto com a instrução em texto
(ex.: "transcreva o texto desta imagem").

```
POST /v1/chat/completions
```

```json
{
  "messages": [
    {
      "role": "user",
      "content": [
        { "type": "text", "text": "Transcreva todo o texto visível nesta imagem." },
        {
          "type": "image_url",
          "image_url": { "url": "data:image/png;base64,<BASE64_DA_IMAGEM>" }
        }
      ]
    }
  ]
}
```

```bash
IMG_B64=$(base64 -i print.png)
curl https://api.trystac.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $STACK_API_KEY" \
  -d @- <<EOF
{
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "Transcreva o texto desta imagem."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,$IMG_B64"}}
      ]
    }
  ]
}
EOF
```

| Campo | Obrigatório | Descrição |
|---|---|---|
| `content[].type = "image_url"` | sim (pra cada imagem) | Marca a parte como imagem |
| `image_url.url` | sim | Data URL `data:<mime>;base64,<dados>` — sem upload prévio, a imagem viaja no próprio corpo |
| Uma parte `type: "text"` | recomendado | A instrução do que fazer com a imagem (ex.: extrair texto, descrever, comparar) |

**Resposta:** mesma forma do chat comum — texto em `choices[0].message.content`.

**System prompt:** como é a rota de chat, vale a regra padrão — sem mensagem `system`, o
system prompt configurado (o da chave ou o da stack) e o RAG são aplicados; com `system`,
o seu substitui os dois.

**Saída estruturada:** funciona aqui também. Adicione `response_format` com um JSON Schema
e a resposta vem como JSON validado, igual ao caso 3 — a diferença é que a entrada é uma
imagem em base64 no corpo, não um PDF em multipart.

**Limites:**
- Corpo total da request (JSON + imagem em base64) até **8 MB**.
- Número de imagens por mensagem tem teto por plano (`max_images_per_prompt` do pod);
  imagens excedentes são recortadas automaticamente e substituídas por um aviso de
  texto, em vez de gerar erro — a request não falha, mas o modelo é avisado de que
  não viu tudo.
- Formato Anthropic (`/v1/messages`) equivalente usa bloco `{"type": "image", "source": {"type": "base64", "media_type": "...", "data": "..."}}`, convertido internamente para o mesmo formato acima.

---

## 3. Documento — OCR / extração estruturada (PDF → JSON)

Endpoint dedicado: recebe um **PDF** (inclusive escaneado — faz OCR internamente) e
devolve um **JSON validado contra o schema que você define**. Diferente do caso 2,
aqui você não descreve o que quer em texto solto: descreve a **forma** da saída via
JSON Schema.

```
POST /v1/documents/extract
Content-Type: multipart/form-data
```

| Campo | Obrigatório | Descrição |
|---|---|---|
| `file` | sim | O PDF |
| `schema` | sim | JSON Schema (como string) descrevendo os campos a extrair |
| `system` | não | Substitui o system prompt configurado (o da chave ou o da stack) |
| `user` | não | Contexto adicional sobre este documento — **soma** à instrução de extração |
| `max_tokens` | não | Teto da resposta. Default 4000, máximo 16000 |

### Request mínima

Só `file` + `schema`. O system prompt configurado é aplicado automaticamente,
resolvido pela chave (o dela, se tiver; o da stack, caso contrário) — você não
envia nada a mais.

```bash
curl -X POST https://api.trystac.com/v1/documents/extract \
  -H "Authorization: Bearer $STACK_API_KEY" \
  -F file=@nota_fiscal.pdf \
  -F 'schema={
        "type": "object",
        "properties": {
          "numero_nota":   {"type": "string"},
          "cnpj_emitente": {"type": ["string", "null"]},
          "valor_total":   {"type": "number"}
        },
        "required": ["numero_nota", "cnpj_emitente", "valor_total"]
      }'
```

### Com `user` — contexto deste documento

Use quando a mesma stack recebe documentos de tipos diferentes e você precisa dizer
*o que é este aqui*. O texto **soma** à instrução de extração, não a substitui.

```bash
curl -X POST https://api.trystac.com/v1/documents/extract \
  -H "Authorization: Bearer $STACK_API_KEY" \
  -F file=@contrato.pdf \
  -F 'schema={
        "type": "object",
        "properties": {
          "locatario":     {"type": "string"},
          "valor_aluguel": {"type": "number"},
          "vencimento":    {"type": ["string", "null"]}
        },
        "required": ["locatario", "valor_aluguel", "vencimento"]
      }' \
  -F 'user=Este é um contrato de locação. A data que importa é a de vencimento, não a de assinatura.'
```

O modelo recebe:

```
system: <system prompt configurado — o da chave ou o da stack>
user:   <instrução padrão de extração — não invente, use null se não achar>
        Contexto adicional informado por quem enviou o documento:
        Este é um contrato de locação. A data que importa é...
        --- DOCUMENTO --- <texto do PDF> --- FIM DO DOCUMENTO ---
```

### Com `system` — substituindo a configuração da plataforma

Use quando esta requisição precisa de regras diferentes das configuradas na plataforma.

```bash
curl -X POST https://api.trystac.com/v1/documents/extract \
  -H "Authorization: Bearer $STACK_API_KEY" \
  -F file=@nota_fiscal.pdf \
  -F 'schema={
        "type": "object",
        "properties": {"valor_total": {"type": "number"}},
        "required": ["valor_total"]
      }' \
  -F 'system=Você extrai dados de notas fiscais brasileiras. Valores sempre em BRL, datas em DD/MM/AAAA.'
```

Os dois campos são combináveis. A regra é a mesma do chat:

| Você envia | O que vale |
|---|---|
| Nada | System prompt configurado — o da chave, se ela tiver; senão o da stack |
| `system` com conteúdo | O seu (substitui o configurado) |
| `system` vazio | O configurado — vazio não substitui nada |
| `user` | Soma à instrução de extração |

A assimetria é proposital: `system` é **configuração** (faz sentido trocar), `user` é
**tarefa** (soma). Se o `user` substituísse, você removeria sem querer a instrução que
impede o modelo de inventar valores.

A base de conhecimento (RAG) **não** é usada neste endpoint: o contexto relevante é o
documento enviado, e trechos de outros documentos aumentariam o risco de preencher um
campo com dado que não está no seu PDF.

**Resposta:**

```json
{
  "data": { "numero_nota": "12345", "cnpj_emitente": "11.222.333/0001-44", "valor_total": 1500.0 },
  "pages": 3,
  "ocr_used": false,
  "usage": { "prompt_tokens": 2104, "completion_tokens": 48 }
}
```

- `data`: seu JSON, já validado contra o `schema`.
- `ocr_used`: `true` se alguma página precisou de OCR (PDF escaneado) — vale conferir
  o resultado com mais atenção nesse caso.

**Regra de schema mais importante:** todo campo em `required` **e** anulável
(`{"type": ["string", "null"]}`) quando puder legitimamente faltar no documento — do
contrário o modelo é forçado a inventar um valor (campo obrigatório e não-anulável)
ou pode omitir o campo em silêncio (campo fora de `required`). Detalhes em
[integracao.md](integracao.md#o-ponto-mais-importante-declare-os-campos-que-podem-faltar).

**Limites:**

| Limite | Go | Pro |
|---|---|---|
| Tamanho do arquivo | 8 MB | 15 MB |
| Páginas por requisição | 15 | 30 |

Schema até 64 KB. Timeout do servidor: 240s (dimensione o timeout do seu cliente
acima disso — documentos escaneados com várias páginas podem levar minutos).

**Erros específicos:**

| Status | Significado |
|---|---|
| `400` | PDF ilegível/corrompido, sem texto extraível, schema inválido, ou documento grande demais para a janela do plano |
| `413` | Arquivo, número de páginas ou schema acima do limite |
| `422` | `max_tokens` fora da faixa aceita |
| `502` | Modelo não devolveu JSON aderente ao schema (resposta inclui `raw_output` para diagnóstico) |

---

## 3.5. Imagem solta — OCR / extração estruturada (JPEG/PNG/WEBP → JSON)

Endpoint irmão do caso 3, para quando o que você tem é uma **imagem solta** (foto de
celular, print de tela, scan avulso) em vez de um PDF. Mesmo contrato de resposta,
mesmas regras de `system`/`user`/schema — só troca o `file` e o formato aceito.

Diferente do caso 2 (que manda a imagem ao modelo dentro do chat), aqui a extração de
texto é feita por **OCR no próprio gateway** (mesmo motor usado no PDF escaneado do
caso 3) — o modelo só recebe o texto já extraído, nunca a imagem em si.

```
POST /v1/images/extract
Content-Type: multipart/form-data
```

| Campo | Obrigatório | Descrição |
|---|---|---|
| `file` | sim | A imagem, em JPEG, PNG ou WEBP |
| `schema` | sim | JSON Schema (como string) descrevendo os campos a extrair |
| `system` | não | Substitui o system prompt configurado (o da chave ou o da stack) |
| `user` | não | Contexto adicional sobre esta imagem — **soma** à instrução de extração |
| `max_tokens` | não | Teto da resposta. Default 4000, máximo 16000 |

```bash
curl -X POST https://api.trystac.com/v1/images/extract \
  -H "Authorization: Bearer $STACK_API_KEY" \
  -F file=@nota_fiscal.jpg \
  -F 'schema={
        "type": "object",
        "properties": {
          "numero_nota":   {"type": "string"},
          "cnpj_emitente": {"type": ["string", "null"]},
          "valor_total":   {"type": "number"}
        },
        "required": ["numero_nota", "cnpj_emitente", "valor_total"]
      }'
```

**Resposta:**

```json
{
  "data": { "numero_nota": "12345", "cnpj_emitente": "11.222.333/0001-44", "valor_total": 1500.0 },
  "pages": 1,
  "ocr_used": true,
  "usage": { "prompt_tokens": 612, "completion_tokens": 48 }
}
```

Diferente do PDF, aqui `pages` é sempre `1` e `ocr_used` é sempre `true`: não existe
"texto embutido" numa imagem solta, toda imagem passa por OCR. A mesma regra de schema
(campo em `required` **e** anulável quando puder faltar) vale igual — ver
[integracao.md](integracao.md#o-ponto-mais-importante-declare-os-campos-que-podem-faltar).

**Limites:**

| Limite | Go | Pro |
|---|---|---|
| Tamanho do arquivo | 5 MB | 10 MB |
| Resolução | 20 megapixels | 20 megapixels |

Schema até 64 KB. Timeout do servidor: 240s.

**Erros específicos:**

| Status | Significado |
|---|---|
| `400` | Imagem ilegível/corrompida, formato não suportado, nenhum texto encontrado por OCR, schema inválido, ou conteúdo grande demais para a janela do plano |
| `413` | Arquivo, resolução ou schema acima do limite |
| `422` | `max_tokens` fora da faixa aceita |
| `502` | Modelo não devolveu JSON aderente ao schema (resposta inclui `raw_output` para diagnóstico) |

---

## 3.9. Geração de imagem

Cria uma imagem a partir de um prompt, ou edita uma imagem existente. Formato
compatível com a OpenAI. **Exige uma chave de uma stack do plano `Image`** — uma
chave de plano de LLM recebe `403`, porque o pod que gera imagem não é o mesmo
que responde chat.

A resposta é sempre `b64_json`. `response_format: "url"` é recusado.

### 3.9.1. Texto → imagem

`POST /v1/images/generations` — `application/json`

```json
{
  "prompt": "um gato astronauta flutuando sobre a Terra",
  "size": "1024x1024",
  "n": 1,
  "steps": 4,
  "guidance_scale": 1.0,
  "seed": 42
}
```

| Campo | Obrigatório | Notas |
|---|---|---|
| `prompt` | sim | texto livre |
| `size` | não | `1024x1024` (padrão), `1536x1024`, `1024x1536` — lista fechada |
| `n` | não | máximo 1 por requisição |
| `steps` | não | padrão 4, teto 8 (o checkpoint é *distilled*) |
| `guidance_scale` | não | padrão 1.0 |
| `seed` | não | inteiro (0 a 2^64-1). Omitido, o servidor sorteia e devolve o valor em `meta.seed` |
| `model` | não | **ignorado**: o servidor fixa o modelo servido |

Resposta:

```json
{
  "created": 1767225600,
  "data": [{ "b64_json": "iVBORw0KGgo..." }],
  "meta": {
    "prompt": "um gato astronauta flutuando sobre a Terra",
    "width": 1024, "height": 1024, "steps": 4,
    "guidance_scale": 1.0, "seed": 42, "n": 1,
    "model": "flux2-klein-4b"
  }
}
```

`meta` traz os parâmetros **efetivos** da geração. O campo mais útil ali é a
`seed`: quando você não manda uma, o servidor sorteia — e é esse valor que
reproduz a mesma imagem depois. Sem ele, uma requisição sem `seed` produziria
uma imagem que ninguém saberia repetir.

A seed é do **lote**, não de cada imagem: com `n > 1`, reproduzir a imagem *i*
exige a mesma `seed` **e** o mesmo `n`.

### 3.9.2. Imagem → imagem

`POST /v1/images/edits` — `multipart/form-data`, até 4 imagens de referência de
15 MiB cada.

```bash
curl https://api.trystac.com/v1/images/edits \
  -H "Authorization: Bearer $STAC_API_KEY" \
  -F "prompt=deixe em preto e branco" \
  -F "image[]=@foto.png"
```

O campo aceita os dois nomes: `image` e `image[]` (este último é o que os
exemplos da OpenAI usam para múltiplas imagens). Formatos aceitos: PNG, JPEG e
WEBP — detectados pelo conteúdo do arquivo, não pela extensão nem pelo
`Content-Type`.

> **`model` nesta rota:** ao contrário do `generations`, aqui o campo **não** é
> corrigido pelo servidor. Omita-o, ou mande exatamente o nome servido — qualquer
> outro valor recebe `404`.

`mask` não é suportado (`400`): este pod carrega um pipeline só, e aplicar a
edição na imagem inteira fingindo respeitar a máscara seria pior que recusar.

**Erros específicos:**

| Status | `code` | Significado |
|---|---|---|
| `400` | `invalid_size`, `invalid_steps`, … | campo fora da faixa ou com tipo errado |
| `400` | `mask_not_supported` | `mask` não é suportado nesta versão |
| `400` | `unsupported_image_format` | o arquivo não é PNG/JPEG/WEBP |
| `400` | `wrong_route_for_reference_image` | mandou `image` no JSON do `generations` — use `/v1/images/edits` |
| `403` | — | a chave não é de uma stack do plano `Image` |
| `404` | `model_not_found` | `model` diferente do servido (só no `edits`) |
| `413` | — | corpo acima do limite da rota |
| `429` | `queue_full` | fila do pod cheia — respeite o `Retry-After` |
| `429` | — | acima do ritmo permitido para a stack (ver abaixo) — respeite o `Retry-After` |
| `503` | `model_not_ready` | máquina ainda carregando, ou degradada |
| `502` | — | a imagem foi gerada mas não foi possível armazená-la (ver abaixo) |
| `504` | `queue_timeout` | espera na fila excedida |

### 3.9.3. Ritmo de requisições

Geração de imagem **não tem cota diária** — você pode gerar quantas imagens
quiser. O que existe é um limite de *ritmo*, porque o pod gera uma imagem por
vez:

| | Valor | O que significa |
|---|---|---|
| Vazão sustentada | **12 por minuto** | o ritmo que a stack mantém indefinidamente |
| Rajada | **4 de uma vez** | quantas cabem simultaneamente antes do `429` |

Os dois limites são **da stack**, não da chave: emitir mais chaves não aumenta a
capacidade, porque quem gera as imagens é a mesma GPU.

A rajada de 4 é a profundidade da fila do pod. Disparar 10 requisições de uma
vez não as torna mais rápidas — 4 entram e 6 voltam `429`. Para volume, o padrão
que funciona é manter até 4 em voo e emendar a próxima quando uma terminar.

O `Retry-After` do `429` diz quantos segundos esperar; com 12/min, um lugar na
fila se abre a cada 5 segundos.

### 3.9.4. Armazenamento e retenção

Toda imagem gerada é guardada, ligada à conta, à stack e à chave que a criou.
Isso é feito no servidor e **não muda nada no seu código** — a resposta continua
sendo `b64_json`.

Duas consequências que valem conhecer:

- **O `200` significa que a imagem foi armazenada.** A gravação acontece antes
  da resposta sair, e adiciona algumas centenas de milissegundos a uma geração
  que já leva alguns segundos.
- **Um `502` pode acontecer com a imagem já gerada.** Se o armazenamento falhar,
  preferimos o erro explícito a devolver a imagem dizendo que ela ficou
  guardada. É seguro repetir a requisição — mas note que ela **gera de novo**, e
  com uma seed nova se você não fixou nenhuma.

**O arquivo é mantido por 30 dias.** Se você precisa da imagem por mais tempo,
guarde o `b64_json` do seu lado — este armazenamento existe para rastreabilidade
e para a visualização na plataforma, não como hospedagem permanente.

---

## 4. PDF a partir de HTML

Endpoint dedicado com **dois modos**, mutuamente exclusivos:

- **Modo direto** (`html`): você já tem o HTML pronto (por exemplo, de uma resposta
  anterior do modelo) e só quer o PDF renderizado. Sem inferência, sem gastar tokens,
  funciona mesmo com a stack pausada.
- **Modo por instrução** (`user` [+ `system`]): você descreve o que quer, o gateway
  chama o modelo pedindo o HTML e já devolve o PDF renderizado — um único request.

```
POST /v1/documents/generate
Content-Type: application/json
```

| Campo | Obrigatório | Descrição |
|---|---|---|
| `html` | um dos dois | O HTML completo a renderizar (modo direto) |
| `user` | um dos dois | O que o documento deve conter (modo por instrução) |
| `system` | não | Só com `user`. Substitui o system prompt configurado (o da chave ou o da stack) |
| `max_tokens` | não | Só com `user`. Teto da resposta do modelo. Default 8000, máximo 16000 |

`html` e `user` são exclusivos — mandar os dois (ou nenhum) responde `400`.

### Modo direto

```bash
curl -X POST https://api.trystac.com/v1/documents/generate \
  -H "Authorization: Bearer $STACK_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"html": "<h1>Relatório</h1><p>conteúdo já pronto</p>"}' \
  -o relatorio.pdf
```

### Modo por instrução

```bash
curl -X POST https://api.trystac.com/v1/documents/generate \
  -H "Authorization: Bearer $STACK_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"user": "Um relatório de vendas de agosto, com uma tabela por região."}' \
  -o relatorio.pdf
```

O gateway chama o modelo pedindo um HTML autossuficiente (ver regra abaixo) e
renderiza a resposta direto, sem uma segunda chamada do seu lado.

`system` e `user` seguem **exatamente a mesma regra** de
[`/v1/documents/extract`](#3-documento--ocr--extração-estruturada-pdf--json):

| Você envia | O que vale |
|---|---|
| Nada | System prompt configurado — o da chave, se ela tiver; senão o da stack |
| `system` com conteúdo | O seu (substitui o configurado) |
| `system` vazio | O configurado — vazio não substitui nada |
| `user` | Soma à instrução padrão de geração (não a substitui) |

Não passa por RAG: o contexto relevante é a instrução que você deu, não a base de
conhecimento da stack.

**Resposta (os dois modos):** o PDF em bytes (`Content-Type: application/pdf`) — não
é JSON, salve direto no arquivo (`-o` no curl, ou o equivalente no seu cliente HTTP).

**O HTML precisa ser autossuficiente — nos dois modos.** O servidor **não busca
nenhum recurso externo**, nada de `<img src="https://...">`, `@import`, fontes
remotas ou qualquer outra URL de rede. Imagens e fontes têm que estar embutidas como
`data:` URI:

```html
<img src="data:image/png;base64,iVBORw0KG...">
```

Um recurso externo não trava a requisição — ele é simplesmente ignorado e o PDF sai
sem ele. No modo por instrução essa regra já vai embutida na instrução que o gateway
manda ao modelo; no modo direto é responsabilidade de quem monta o HTML.

**Limites:**

| Limite | Go | Pro / Max / Enterprise |
|---|---|---|
| Tamanho do HTML (direto, ou gerado pelo modelo) | 2 MB | 5 MB |
| Páginas no PDF resultante | 20 | 50 |

**Erros específicos:**

| Status | Significado |
|---|---|
| `400` | `html`+`user` combinados, nenhum dos dois enviado, HTML não pôde ser renderizado, ou (modo por instrução) resposta do modelo truncada |
| `413` | HTML (enviado ou gerado pelo modelo) ou número de páginas do PDF resultante acima do limite |
| `422` | `max_tokens` fora da faixa aceita |
| `429` | Muitas gerações simultâneas — tente de novo em seguida |
| `502` | (modo por instrução) falha ao chamar o modelo |

---

## Erros comuns a todos os endpoints

| Status | Causa |
|---|---|
| `401` | Chave ausente, inválida, revogada ou expirada |
| `402` | Assinatura suspensa ou em atraso há mais de 72h — a chave continua válida e volta a responder sozinha assim que o pagamento entrar (até 1 min de propagação). **Não tente de novo**: repetir não resolve, e o `detail` da resposta traz o que fazer |
| `403` | Limite de ambientes simultâneos do plano atingido. Um ambiente é ferramenta + rede de origem; a vaga é liberada sozinha após 14 dias sem uso, ou na hora pelo painel. **Não tente de novo**: nenhuma tentativa a mais libera vaga — veja [Quantos lugares o seu plano conecta](integracao.md#quantos-lugares-o-seu-plano-conecta) |
| `404` | Caminho fora da lista de rotas suportadas |
| `429` / `503` | Rate limit, cota diária ou infraestrutura religando — veja [Retry](integracao.md#retry-o-ponto-mais-importante-para-produção) |
