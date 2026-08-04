# API Reference

Referência rápida das requests para cada caso de uso do serviço. Para explicações
detalhadas (retry, streaming, limites, exemplos em várias linguagens), veja
[integracao.md](integracao.md) — este documento é só o "o que mandar" de cada rota.

Em todos os casos:

| | |
|---|---|
| Base URL | `https://llmserve-docker.up.railway.app` |
| Autenticação | header `Authorization: Bearer <SUA_CHAVE_DE_ACESSO>` |
| Content-Type | `application/json`, exceto `/v1/documents/extract` (`multipart/form-data`) |

---

## 1. Mensagem (chat)

Conversa de texto simples, formato compatível com a OpenAI.

```
POST /v1/chat/completions
```

```json
{
  "model": "vibecoder-base",
  "max_tokens": 8000,
  "messages": [
    { "role": "user", "content": "oi" }
  ]
}
```

```bash
curl https://llmserve-docker.up.railway.app/v1/chat/completions \
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
curl https://llmserve-docker.up.railway.app/v1/chat/completions \
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
system prompt da sua stack (e o RAG) são aplicados; com `system`, o seu substitui os dois.

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
| `system` | não | Substitui o system prompt configurado na sua stack |
| `user` | não | Contexto adicional sobre este documento — **soma** à instrução de extração |
| `max_tokens` | não | Teto da resposta. Default 4000, máximo 16000 |

### Request mínima

Só `file` + `schema`. O system prompt configurado na sua stack é aplicado
automaticamente, resolvido pela chave — você não envia nada a mais.

```bash
curl -X POST https://llmserve-docker.up.railway.app/v1/documents/extract \
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
curl -X POST https://llmserve-docker.up.railway.app/v1/documents/extract \
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
system: <system prompt da sua stack>
user:   <instrução padrão de extração — não invente, use null se não achar>
        Contexto adicional informado por quem enviou o documento:
        Este é um contrato de locação. A data que importa é...
        --- DOCUMENTO --- <texto do PDF> --- FIM DO DOCUMENTO ---
```

### Com `system` — substituindo a configuração da stack

Use quando esta requisição precisa de regras diferentes das configuradas na plataforma.

```bash
curl -X POST https://llmserve-docker.up.railway.app/v1/documents/extract \
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
| Nada | System prompt da stack |
| `system` com conteúdo | O seu (substitui o da stack) |
| `system` vazio | O da stack — vazio não substitui nada |
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

| Limite | VibeCoder | Pro |
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

## Erros comuns a todos os endpoints

| Status | Causa |
|---|---|
| `401` | Chave ausente, inválida, revogada ou expirada |
| `404` | Caminho fora da lista de rotas suportadas |
| `429` / `503` | Rate limit, cota diária ou infraestrutura religando — veja [Retry](integracao.md#retry-o-ponto-mais-importante-para-produção) |
