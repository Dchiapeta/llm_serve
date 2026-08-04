# Integração — chamando o modelo do seu código

Este documento é entregável ao cliente. Ele mostra como chamar o modelo a partir de
qualquer linguagem, com a biblioteca HTTP que ela já tem.

**Não é preciso instalar SDK nenhum.** O endpoint é uma API HTTP compatível com o
formato da OpenAI: você monta um JSON, faz um `POST` e lê a resposta. Se preferir
usar os SDKs `openai` ou `anthropic`, eles também funcionam apontando a base URL —
mas nada aqui depende disso.

## Conexão

| | |
|---|---|
| Endpoint | `POST https://llmserve-docker.up.railway.app/v1/chat/completions` |
| Autenticação | header `Authorization: Bearer <SUA_CHAVE_DE_ACESSO>` |
| Corpo | JSON com `model`, `max_tokens` e `messages` |
| Resposta | o texto vem em `choices[0].message.content` |

A chave de acesso é exibida **uma única vez**, no momento em que é gerada — guarde-a
num gerenciador de segredos. Não é possível recuperá-la depois, apenas gerar uma nova.
Nunca a coloque em código versionado nem em frontend: ela dá acesso direto ao seu plano.

### A request

```http
POST /v1/chat/completions HTTP/1.1
Host: llmserve-docker.up.railway.app
Content-Type: application/json
Authorization: Bearer <SUA_CHAVE_DE_ACESSO>

{
  "model": "go-base",
  "max_tokens": 8000,
  "messages": [{"role": "user", "content": "oi"}]
}
```

O campo `model` é **livre**: o serviço sempre usa o modelo do seu plano, seja qual for
o valor enviado. Ele é obrigatório apenas porque o formato o exige — você não precisa
descobrir nem acertar o nome. Se quiser o nome real, consulte `GET /v1/models`.

### Outras rotas

Além de `/v1/chat/completions`, o serviço aceita `/v1/completions`, `/v1/embeddings`,
`/v1/responses` e `GET /v1/models` (formato OpenAI), `/v1/messages` e
`/v1/messages/count_tokens` (formato Anthropic), e os endpoints dedicados
`/v1/documents/extract` (PDF → JSON) e `/v1/documents/generate` (HTML → PDF, ver
["Geração de PDF a partir de HTML"](#geração-de-pdf-a-partir-de-html)). Qualquer outro
caminho responde `404`.

---

## Exemplos

Todos os exemplos abaixo fazem a mesma chamada e leem a chave da variável de ambiente
`STACK_API_KEY`.

### curl

```bash
curl https://llmserve-docker.up.railway.app/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $STACK_API_KEY" \
  -d '{
    "model": "go-base",
    "max_tokens": 8000,
    "messages": [{"role": "user", "content": "oi"}]
  }'
```

No formato Anthropic, se preferir:

```bash
curl https://llmserve-docker.up.railway.app/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: $STACK_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "go-base",
    "max_tokens": 8000,
    "messages": [{"role": "user", "content": "oi"}]
  }'
```

### Python

```python
# pip install requests
import os
import requests

message = "content here"

r = requests.post(
    "https://llmserve-docker.up.railway.app/v1/chat/completions",
    headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + os.environ["STACK_API_KEY"],
    },
    json={"messages": [{"role": "user", "content": message}]},
)
r.raise_for_status()
print(r.json()["choices"][0]["message"]["content"])
```

Sem `timeout` aqui de propósito — é o exemplo mais simples possível. Para produção,
veja a seção [Retry](#retry-o-ponto-mais-importante-para-produção): a primeira
chamada depois de um período parado pode levar dezenas de segundos religando a
infraestrutura, e lá o timeout generoso (120s) é parte do tratamento, não um
detalhe cosmético.

`model` e `max_tokens` são opcionais: se você não mandar, o serviço usa o modelo do
seu plano e um teto de tokens padrão. Só mande esses campos se quiser controlar
explicitamente o teto de tokens da resposta.

`requests` não faz parte da biblioteca padrão. Para evitar a dependência, dá para usar
`urllib.request` com `json.dumps()` no corpo — a request é a mesma.

### JavaScript / TypeScript

```js
// fetch nativo: Node 18+, Deno, Bun, browser. Sem dependências.
const r = await fetch("https://llmserve-docker.up.railway.app/v1/chat/completions", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    Authorization: "Bearer " + process.env.STACK_API_KEY,
  },
  body: JSON.stringify({
    model: "go-base",
    max_tokens: 8000,
    messages: [{ role: "user", content: "oi" }],
  }),
})
if (!r.ok) throw new Error("HTTP " + r.status)

const data = await r.json()
console.log(data.choices[0].message.content)
```

Rode isso **no servidor**, não no browser: qualquer chave embutida em código de frontend
fica visível para quem abrir o DevTools.

### PHP

```php
<?php
$ch = curl_init("https://llmserve-docker.up.railway.app/v1/chat/completions");
curl_setopt_array($ch, [
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_POST => true,
    CURLOPT_TIMEOUT => 120,
    CURLOPT_HTTPHEADER => [
        "Content-Type: application/json",
        "Authorization: Bearer " . getenv("STACK_API_KEY"),
    ],
    CURLOPT_POSTFIELDS => json_encode([
        "model" => "go-base",
        "max_tokens" => 8000,
        "messages" => [["role" => "user", "content" => "oi"]],
    ]),
]);
$data = json_decode(curl_exec($ch), true);
curl_close($ch);

echo $data["choices"][0]["message"]["content"];
```

### Go

```go
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"time"
)

func main() {
	body, _ := json.Marshal(map[string]any{
		"model":      "go-base",
		"max_tokens": 8000,
		"messages": []map[string]string{
			{"role": "user", "content": "oi"},
		},
	})

	req, _ := http.NewRequest("POST",
		"https://llmserve-docker.up.railway.app/v1/chat/completions",
		bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+os.Getenv("STACK_API_KEY"))

	resp, err := (&http.Client{Timeout: 120 * time.Second}).Do(req)
	if err != nil {
		panic(err)
	}
	defer resp.Body.Close()

	var out map[string]any
	json.NewDecoder(resp.Body).Decode(&out)

	choice := out["choices"].([]any)[0].(map[string]any)
	fmt.Println(choice["message"].(map[string]any)["content"])
}
```

### Java

```java
// Java 11+. HttpClient é stdlib; parsing de JSON não é —
// para ler o campo content use Jackson ou Gson.
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

String body = """
    {"model": "go-base",
     "max_tokens": 8000,
     "messages": [{"role": "user", "content": "oi"}]}
    """;

HttpRequest req = HttpRequest.newBuilder()
    .uri(URI.create("https://llmserve-docker.up.railway.app/v1/chat/completions"))
    .header("Content-Type", "application/json")
    .header("Authorization", "Bearer " + System.getenv("STACK_API_KEY"))
    .timeout(Duration.ofSeconds(120))
    .POST(HttpRequest.BodyPublishers.ofString(body))
    .build();

HttpResponse<String> res = HttpClient.newHttpClient()
    .send(req, HttpResponse.BodyHandlers.ofString());

System.out.println(res.body()); // JSON: choices[0].message.content
```

### C#

```csharp
// .NET 6+
using System.Net.Http.Json;
using System.Text.Json;

using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(120) };
http.DefaultRequestHeaders.Add(
    "Authorization", "Bearer " + Environment.GetEnvironmentVariable("STACK_API_KEY"));

var res = await http.PostAsJsonAsync(
    "https://llmserve-docker.up.railway.app/v1/chat/completions", new
{
    model = "go-base",
    max_tokens = 8000,
    messages = new[] { new { role = "user", content = "oi" } },
});
res.EnsureSuccessStatusCode();

var json = await res.Content.ReadFromJsonAsync<JsonElement>();
Console.WriteLine(json.GetProperty("choices")[0]
    .GetProperty("message").GetProperty("content").GetString());
```

---

## Streaming

Para receber o texto token a token, adicione `"stream": true` ao corpo. A resposta vira
um fluxo SSE: linhas começando com `data: `, cada uma com um pedaço em
`choices[0].delta.content`, encerrando com `data: [DONE]`.

```python
import json, os, requests

message = "content here"

r = requests.post(
    "https://llmserve-docker.up.railway.app/v1/chat/completions",
    headers={"Authorization": "Bearer " + os.environ["STACK_API_KEY"]},
    json={
        "stream": True,
        "messages": [{"role": "user", "content": message}],
    },
    stream=True,
)
r.raise_for_status()

for linha in r.iter_lines():
    if not linha or not linha.startswith(b"data: "):
        continue
    dado = linha[len(b"data: "):]
    if dado == b"[DONE]":
        break
    pedaco = json.loads(dado)["choices"][0]["delta"].get("content")
    if pedaco:
        print(pedaco, end="", flush=True)
```

---

## Ferramentas de código

Se em vez de escrever código você quer usar uma CLI, não precisa de request nenhuma —
só de configuração.

### Claude Code

```bash
export ANTHROPIC_BASE_URL="https://llmserve-docker.up.railway.app"
export ANTHROPIC_AUTH_TOKEN="<SUA_CHAVE_DE_ACESSO>"
export ANTHROPIC_API_KEY=""
export ANTHROPIC_MODEL="go-base"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$ANTHROPIC_MODEL"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$ANTHROPIC_MODEL"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$ANTHROPIC_MODEL"
export CLAUDE_CODE_AUTO_COMPACT_WINDOW=120000
claude
```

`CLAUDE_CODE_AUTO_COMPACT_WINDOW` não é opcional: o Claude Code assume uma janela de
200k e não tem como descobrir a real do seu plano. Sem essa variável ele só percebe o
limite quando a chamada é recusada; com ela, compacta a conversa sozinho por volta de
80% da janela. Use **120000** — a página da sua máquina, na aba **Ferramentas**, mostra
o valor já preenchido.

Esse número é a *capacidade* que o Claude Code passa a assumir, não o ponto em que ele
compacta: ele compacta um pouco antes de acreditar que encheu. Por isso 120000, e não
os 104000 que seriam 80% de 131072 — declarar 80% faria a compactação acontecer bem
antes disso, desperdiçando contexto. 120000 é o teto: o resto da janela fica reservado
para a resposta do modelo.

Esses `export` valem só para a sessão de terminal em que você os rodou. Para não
depender disso, ponha o mesmo conteúdo em `~/.claude/settings.json`:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://llmserve-docker.up.railway.app",
    "ANTHROPIC_AUTH_TOKEN": "<SUA_CHAVE_DE_ACESSO>",
    "ANTHROPIC_API_KEY": "",
    "ANTHROPIC_MODEL": "go-base",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "go-base",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "go-base",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "go-base",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "120000"
  }
}
```

Dois detalhes do formato: os valores são **strings** (`"120000"`, não `120000`) e não
há interpolação — `"$ANTHROPIC_MODEL"` não funciona em JSON, por isso o nome do modelo
aparece repetido. Use o `~/.claude/settings.json` da sua conta, **não** o
`.claude/settings.json` do projeto: esse costuma ir para o git e levaria a sua chave
de acesso com ele.

Se ainda assim o contexto estourar, o gateway responde com um erro explicando o que
fazer. Nesse ponto use `/clear` e comece uma sessão nova — `/compact` reenvia a
conversa inteira para o modelo, então é justamente o comando que não cabe mais na
janela.

### Codex CLI

Em `~/.codex/config.toml`:

```toml
model_provider = "llmserve"
model = "go-base"

# Compacta o histórico antes de estourar a janela do plano. O Codex assume 200k
# por padrão e só descobriria o limite ao ser recusado. Mesmo valor do Claude
# Code: o maior input que ainda deixa espaço para a resposta na janela de 131072.
model_auto_compact_token_limit = 120000

[model_providers.llmserve]
name = "llmserve"
base_url = "https://llmserve-docker.up.railway.app/v1"
env_key = "LLMSERVE_API_KEY"
wire_api = "responses"
```

A chave vai na variável de ambiente `LLMSERVE_API_KEY`.

Não configure `model_context_window`: há um bug conhecido do Codex
([openai/codex#16068](https://github.com/openai/codex/issues/16068)) em que essa chave
quebra a auto-compaction de forma permanente após o primeiro overflow — use só o
`model_auto_compact_token_limit`. Se a sua versão do Codex ignorar a config (comportamento
reportado em algumas builds), o fallback é `/compact` manual em sessões longas.

### Cursor, Cline, Continue e outras

Todas têm um provider do tipo "OpenAI compatible". A configuração é sempre a mesma:

- **Base URL**: `https://llmserve-docker.up.railway.app/v1`
- **API key**: sua chave de acesso
- **Model**: qualquer valor

---

## Limites e comportamento

O serviço normaliza toda request antes de processá-la. Nada abaixo é erro: é o
comportamento esperado, e conhecê-lo evita horas de depuração.

| Parâmetro | Comportamento |
|---|---|
| `model` | Sempre substituído pelo modelo do seu plano |
| `max_tokens` | Piso de **8000** e teto de **16000** |
| `n` | Sempre `1` |
| `logit_bias` | Removido |
| `temperature`, `top_p`, `frequency_penalty`, `presence_penalty` | Limitados às faixas válidas |
| Roles das mensagens | Só `system`, `user`, `assistant` e `tool`; outras são descartadas |

Dois pontos que costumam surpreender:

**O piso de `max_tokens` consome a janela de contexto.** Como a resposta pode ocupar até
8000 tokens, esse espaço sai do total disponível — o texto de entrada que cabe é a janela
do plano menos essa reserva. Um prompt muito longo é recusado mesmo parecendo caber.

**Se você enviar uma mensagem `system`, a sua é usada.** O system prompt configurado na
sua conta e o contexto de base de conhecimento (RAG) não são aplicados nessa chamada.
Isso é intencional — ferramentas como Cursor e Claude Code embutem o próprio system
prompt e quebrariam se recebessem outro por cima. Se você depende do system prompt
configurado na conta, **não envie um `system`**.

A regra completa, incluindo o caso do `system` sem conteúdo:

| O que você envia | O que vale |
|---|---|
| Nenhuma mensagem `system` | System prompt + RAG configurados na sua stack |
| `system` com conteúdo | O seu conteúdo (a configuração da stack e o RAG não entram) |
| `system` vazio, ou sem nenhuma parte de texto | System prompt + RAG da stack — um `system` sem instrução não substitui nada |

O `content` do `system` pode ser string ou lista de partes
(`[{"type": "text", "text": "..."}]`); os dois formatos são lidos igualmente. Partes que
não são texto (imagem, por exemplo) são ignoradas para esse fim.

Nada disso exige mandar nada além da chave: é ela que identifica a sua stack, e a
configuração viaja junto automaticamente.

---

## Extração estruturada de documento (PDF → JSON)

Além dos endpoints compatíveis com OpenAI, existe um endpoint dedicado que recebe um
**PDF** e devolve um **JSON no formato que você definir**. Você não precisa extrair o
texto do documento: o serviço faz isso (inclusive OCR, para PDF escaneado) e garante que
a saída obedece ao seu schema.

```
POST /v1/documents/extract
Content-Type: multipart/form-data
```

| Campo | Obrigatório | Descrição |
|---|---|---|
| `file` | sim | O PDF |
| `schema` | sim | JSON Schema (como string) descrevendo os campos a extrair |
| `system` | não | Substitui o system prompt configurado na sua stack (mesma regra do chat) |
| `user` | não | Contexto adicional sobre este documento — **soma** à instrução de extração |
| `max_tokens` | não | Teto da resposta. Default 4000, máximo 16000 |

```bash
curl -X POST https://SEU-GATEWAY/v1/documents/extract \
  -H "Authorization: Bearer $STAC_API_KEY" \
  -F file=@nota_fiscal.pdf \
  -F 'schema={
        "type": "object",
        "properties": {
          "numero_nota":   {"type": "string"},
          "cnpj_emitente": {"type": ["string", "null"]},
          "valor_total":   {"type": "number"}
        },
        "required": ["numero_nota", "valor_total"]
      }'
```

Resposta:

```json
{
  "data": { "numero_nota": "12345", "cnpj_emitente": "11.222.333/0001-44", "valor_total": 1500.0 },
  "pages": 3,
  "ocr_used": false,
  "usage": { "prompt_tokens": 2104, "completion_tokens": 48 }
}
```

`data` é o seu JSON, **já validado contra o schema** — se ele voltar, adere ao formato que
você pediu. `ocr_used` indica se alguma página precisou de OCR (PDF escaneado): quando
`true`, vale conferir o resultado com mais atenção, porque a qualidade depende da imagem.

### O ponto mais importante: declare os campos que podem faltar

A saída é forçada a obedecer ao seu schema **token a token**. Isso é o que garante JSON
válido — mas tem uma consequência que decide a qualidade do resultado:

> Um campo declarado `{"type": "string"}` **não pode** voltar `null`. A gramática proíbe.
> Se a informação não estiver no documento, o modelo é obrigado a emitir *alguma* string
> — e vai **inventar** uma.

Ou seja: o schema não é só o formato da resposta, é a definição do que é aceitável. Para
um campo que pode legitimamente não existir no documento, declare-o anulável:

```json
"cnpj_emitente": {"type": ["string", "null"]}
```

Assim o modelo tem como dizer "não achei" — e a instrução que enviamos ao modelo pede
exatamente isso (omitir ou usar `null` em vez de preencher com um valor plausível).

Só que existe uma segunda armadilha, na direção oposta:

> **Campo fora de `required` é omitido em silêncio.** Pelo JSON Schema, campo não
> obrigatório é opcional — a gramática permite não emitir a chave, e o modelo faz o
> caminho mais barato: some com o campo em vez de procurar o dado. Você recebe `200`
> com JSON válido e um campo a menos, sem aviso nenhum.

A combinação que resolve as duas é **`required` em todos os campos** + **tipo anulável**
naqueles que podem legitimamente faltar:

```json
{
  "type": "object",
  "properties": {
    "numero_nota":   {"type": "string"},
    "cnpj_emitente": {"type": ["string", "null"]},
    "valor_total":   {"type": "number"}
  },
  "required": ["numero_nota", "cnpj_emitente", "valor_total"]
}
```

Assim o campo **sempre** aparece na resposta e vem `null` quando realmente não está no
documento. Deixe fora de `required` apenas o que você aceita não receber.

O mesmo vale **dentro** de arrays e objetos aninhados: cada objeto de uma lista precisa do
próprio `required`, senão os itens voltam com campos faltando pelo mesmo motivo.

### `system` e `user`: a mesma regra do chat

Os papéis funcionam aqui exatamente como em `/v1/chat/completions` — só viajam como campos
do multipart, porque há um arquivo na mesma requisição.

| Você envia | O que acontece |
|---|---|
| Nada | Vale o system prompt configurado na sua stack (resolvido pela chave) |
| `system` com conteúdo | Substitui o system prompt da stack |
| `system` vazio | Vale o da stack — vazio não substitui nada |
| `user` | **Soma** à instrução de extração, não a substitui |

```bash
curl -X POST https://SEU-GATEWAY/v1/documents/extract \
  -H "Authorization: Bearer $STAC_API_KEY" \
  -F file=@contrato.pdf \
  -F 'schema={...}' \
  -F 'user=Este é um contrato de locação. A data que importa é a de vencimento.'
```

A assimetria entre os dois é proposital: `system` é **configuração** (faz sentido trocar),
`user` é **tarefa** (soma). Se o `user` substituísse, você removeria sem querer a instrução
que impede o modelo de inventar valores — a garantia mais importante da extração.

A base de conhecimento (RAG) **não** é usada neste endpoint, de propósito: aqui o contexto
relevante é o documento que você enviou, e trechos de outros documentos aumentariam o
risco de um campo ser preenchido com dado que não está no seu PDF.

### Limites

| Limite | Go | Pro |
|---|---|---|
| Tamanho do arquivo | 8 MB | 15 MB |
| Páginas por requisição | 15 | 30 |

O schema em si tem teto de 64 KB. Documento muito grande é recusado com `400` explicando
quantos tokens ele ocupou — nesse caso, divida o PDF e envie por partes.

### Erros específicos

| Status | Significado |
|---|---|
| `400` | PDF ilegível/corrompido, sem texto extraível, schema inválido, ou documento grande demais para a janela do plano |
| `413` | Arquivo, número de páginas ou schema acima do limite |
| `422` | `max_tokens` fora da faixa aceita (precisa ser maior que 0 e no máximo 16000) |
| `502` | O modelo não devolveu JSON aderente ao schema (a resposta inclui `raw_output` para diagnóstico) |

Um `502` com `raw_output` **truncado no meio** costuma significar que a resposta não caberia
no espaço restante da janela: o documento é grande e sobrou pouco para o JSON. Nesse caso,
divida o PDF ou reduza o número de campos do schema.

**Sobre latência:** a extração é síncrona e inclui OCR quando necessário, então um
documento de muitas páginas escaneadas pode levar minutos. O teto do servidor é de 240s
para a inferência — dimensione o timeout do seu cliente acima disso.

---

## Extração estruturada de imagem (JPEG/PNG/WEBP → JSON)

Endpoint irmão do de PDF, para quando o que você tem é uma **imagem solta** — foto de
celular, print de tela, scan avulso — em vez de um PDF. Mesma ideia: você define o schema,
o serviço faz OCR e devolve o JSON.

```
POST /v1/images/extract
Content-Type: multipart/form-data
```

| Campo | Obrigatório | Descrição |
|---|---|---|
| `file` | sim | A imagem, em JPEG, PNG ou WEBP |
| `schema` | sim | JSON Schema (como string) descrevendo os campos a extrair |
| `system` | não | Substitui o system prompt configurado na sua stack (mesma regra do chat) |
| `user` | não | Contexto adicional sobre esta imagem — **soma** à instrução de extração |
| `max_tokens` | não | Teto da resposta. Default 4000, máximo 16000 |

```bash
curl -X POST https://SEU-GATEWAY/v1/images/extract \
  -H "Authorization: Bearer $STAC_API_KEY" \
  -F file=@nota_fiscal.jpg \
  -F 'schema={
        "type": "object",
        "properties": {
          "numero_nota":   {"type": "string"},
          "cnpj_emitente": {"type": ["string", "null"]},
          "valor_total":   {"type": "number"}
        },
        "required": ["numero_nota", "valor_total"]
      }'
```

Resposta:

```json
{
  "data": { "numero_nota": "12345", "cnpj_emitente": "11.222.333/0001-44", "valor_total": 1500.0 },
  "pages": 1,
  "ocr_used": true,
  "usage": { "prompt_tokens": 612, "completion_tokens": 48 }
}
```

Contrato de resposta idêntico ao de PDF (mesmo `data`, mesmo `usage`), com duas
diferenças fixas por não haver conceito de "página" numa imagem solta: `pages` é sempre
`1` e `ocr_used` é sempre `true` — ao contrário do PDF, aqui **toda** imagem passa por
OCR, não há "texto embutido" a extrair primeiro.

`system` e `user` seguem exatamente a mesma regra do endpoint de PDF (ver seção acima) —
o mesmo vale para a orientação sobre **declarar campos anuláveis** no schema, para evitar
que o modelo invente um valor quando a informação não está na imagem.

### Limites

| Limite | Go | Pro |
|---|---|---|
| Tamanho do arquivo | 5 MB | 10 MB |
| Resolução | 20 megapixels | 20 megapixels |

O schema em si tem o mesmo teto de 64 KB do endpoint de PDF.

### Erros específicos

| Status | Significado |
|---|---|
| `400` | Imagem ilegível/corrompida, formato não suportado, nenhum texto encontrado por OCR, schema inválido, ou conteúdo grande demais para a janela do plano |
| `413` | Arquivo, resolução ou schema acima do limite |
| `422` | `max_tokens` fora da faixa aceita (precisa ser maior que 0 e no máximo 16000) |
| `502` | O modelo não devolveu JSON aderente ao schema (a resposta inclui `raw_output` para diagnóstico) |

**Sobre latência:** mesma disciplina do endpoint de PDF — a extração é síncrona e inclui
OCR sempre, então dimensione o timeout do seu cliente acima dos 240s de teto do servidor.

---

## Geração de PDF a partir de HTML

`POST /v1/documents/generate` tem **dois modos**, mutuamente exclusivos — mandar os
dois campos, ou nenhum, responde `400`:

- **`html`** (modo direto): você já tem o HTML pronto (de uma resposta de chat
  anterior, por exemplo) e só quer o PDF renderizado. **Não há inferência
  envolvida** — o endpoint só faz a rasterização. Isso muda duas coisas na prática:
  (1) funciona mesmo com a stack/pod pausado por ociosidade, já que nenhum modelo é
  chamado; (2) não conta contra sua quota de tokens.
- **`user`** (+ `system` opcional; modo por instrução): você descreve o que o
  documento deve conter, e o próprio gateway chama o modelo pedindo o HTML e
  renderiza a resposta — um único request em vez de dois.

### Modo direto

```bash
curl -X POST https://llmserve-docker.up.railway.app/v1/documents/generate \
  -H "Authorization: Bearer $STACK_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"html": "<h1>Relatório mensal</h1><p>...</p>"}' \
  -o relatorio.pdf
```

### Modo por instrução

```bash
curl -X POST https://llmserve-docker.up.railway.app/v1/documents/generate \
  -H "Authorization: Bearer $STACK_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"user": "Um relatório mensal de vendas, com uma tabela por região."}' \
  -o relatorio.pdf
```

Aqui `system` e `user` seguem **a mesma regra** de `/v1/documents/extract`: sem
`system`, vale o system prompt configurado na sua stack; com `system` (não vazio), ele
substitui o da stack; `user` sempre **soma** à instrução padrão de geração (que já
inclui a regra de HTML autossuficiente abaixo — você não precisa repetir isso na sua
instrução).

A resposta, nos dois modos, é o PDF em bytes (`Content-Type: application/pdf`), não
JSON.

**Regra mais importante: o HTML precisa ser autossuficiente — nos dois modos.** O
motor de renderização (WeasyPrint) roda no mesmo processo que atende toda a sua conta
e as de outros clientes, na rede privada do provedor de hosting — por isso o servidor
**nunca busca URL nenhuma** referenciada no HTML (`<img src="https://...">`,
`@import`, fonte remota). Qualquer recurso assim é silenciosamente ignorado, não trava
a requisição. Para imagens e fontes, embuta como `data:` URI:

```html
<img src="data:image/png;base64,iVBORw0KG...">
```

No modo por instrução essa regra já vai embutida no prompt que o gateway manda ao
modelo. No modo direto, se o HTML vier de uma resposta separada do modelo com
`<img src="https://...">` apontando para uma URL real, peça a ele (via prompt) para
converter em `data:` URI, ou converta você mesmo antes de mandar para este endpoint.

### Limites

| Limite | Go | Pro / Max / Enterprise |
|---|---|---|
| Tamanho do HTML (enviado, ou gerado pelo modelo) | 2 MB | 5 MB |
| Páginas no PDF resultante | 20 | 50 |

O teto de páginas existe porque CSS pode gerar muito mais páginas do que o HTML
"parece" pedir (ex.: `page-break-after` repetido) — é checado depois do layout, mas
antes de gerar os bytes finais do PDF, então uma tentativa de estourar o limite falha
sem custo extra de CPU. No modo por instrução, o HTML devolvido pelo modelo passa pelo
mesmo teto de tamanho do modo direto — nada garante que o modelo respeitou a instrução.

### Erros específicos

| Status | Significado |
|---|---|
| `400` | `html` e `user` combinados, nenhum dos dois enviado, HTML não pôde ser renderizado, ou (modo por instrução) resposta do modelo truncada por falta de espaço |
| `413` | HTML (enviado ou gerado pelo modelo) ou páginas do PDF resultante acima do limite do plano |
| `422` | `max_tokens` fora da faixa aceita (só se aplica ao modo por instrução) |
| `429` | Muitas gerações simultâneas nesse instante — o servidor prioriza falhar rápido em vez de enfileirar; tente de novo |
| `502` | (modo por instrução) falha ao chamar o modelo, ou resposta fora do formato esperado |

---

## Reasoning ("thinking"): por que a resposta pode demorar

O modelo do seu plano pode ser um modelo de *reasoning*: antes de responder, ele gera um
bloco de raciocínio interno (chain-of-thought) que não aparece pra você, mas consome
tokens e tempo de geração como qualquer outro texto. Uma pergunta simples pode gerar
centenas ou milhares de tokens de raciocínio antes da resposta final — é normal a mesma
pergunta levar de poucos segundos a mais de um minuto dependendo de quanto o modelo
"pensa".

Isso importa em dois cenários:

- **Latência**: se sua aplicação é sensível a tempo de resposta (ex.: classificação em
  lote, chat em tempo real), o reasoning pode ser o maior custo de tempo da chamada —
  maior até que a resposta em si.
- **Requests sem streaming**: sem `"stream": true`, você só recebe a resposta depois que
  toda a geração (raciocínio incluído) termina. Se isso passar de ~60 segundos, a chamada
  pode ser encerrada pelo gateway antes de qualquer byte chegar (ver
  [Retry](#retry-o-ponto-mais-importante-para-produção)). Para tarefas onde o reasoning
  pode ser longo, use streaming — ou desligue o reasoning, abaixo.

### Desligando o reasoning por request

Para modelos da família Qwen3.x, dá pra desligar o thinking numa chamada específica com
`chat_template_kwargs`:

```json
{
  "messages": [{"role": "user", "content": "Classifique: promocional, suporte ou outro."}],
  "chat_template_kwargs": {"enable_thinking": false}
}
```

**O campo precisa estar aninhado dentro de `chat_template_kwargs`** — mandar
`"enable_thinking": false` solto na raiz do corpo é ignorado silenciosamente (o vLLM não
reconhece o campo nesse nível, e como não é um parâmetro do padrão OpenAI, nada acusa
erro). Esse é o erro mais comum ao tentar essa configuração: a chamada continua
"pensando" normalmente porque a flag nunca chegou a valer.

Com o thinking desligado, uma classificação simples cai de potencialmente dezenas de
segundos (milhares de tokens de raciocínio) para poucos tokens de resposta direta — mas
a qualidade do resultado em tarefas mais abertas ou que exigem múltiplos passos de
raciocínio tende a piorar. Vale mais a pena em tarefas fechadas e objetivas
(classificação, extração, formatação) do que em tarefas abertas.

---

## Retry: o ponto mais importante para produção

A infraestrutura do seu plano pode estar pausada por inatividade. A primeira chamada
depois de um período parado **religa a máquina e responde `503`**, com o header
`Retry-After` dizendo quantos segundos esperar. É o funcionamento normal, não uma falha.

| Status | `Retry-After` | Significado |
|---|---|---|
| `503` | ~60s | Infraestrutura religando após pausa por inatividade |
| `503` | ~5s | Preparo de recursos do seu plano em andamento |
| `503` | ~5s | Sem capacidade no momento — muitas requisições concorrentes esgotaram as vagas do seu plano |
| `429` | variável | Limite de requisições por minuto atingido |
| `429` | 3600s | Cota diária de tokens do plano esgotada |

O terceiro caso é diferente dos outros dois: não é a infraestrutura subindo, é volume —
seu código (ou um teste de carga) mandou mais requisições simultâneas do que o plano
comporta. É o cenário típico de processar uma fila grande (milhares de itens) sem limitar
a concorrência. A mensagem no corpo do `503` (`detail`) diz explicitamente qual dos dois
motivos ocorreu.

Como você está chamando a API diretamente, **o retry é responsabilidade do seu código**.
Sem ele, a primeira chamada do dia falha na cara do seu usuário — e, no caso de excesso de
concorrência, **cada item da fila que cair num `503` é perdido permanentemente** em vez de
ser reprocessado. O padrão é simples: repetir enquanto o status for `429` ou `503`,
dormindo o que o `Retry-After` mandar.

```python
import os, time, requests

URL = "https://llmserve-docker.up.railway.app/v1/chat/completions"
HEADERS = {
    "Content-Type": "application/json",
    "Authorization": "Bearer " + os.environ["STACK_API_KEY"],
}

def chamar(payload, tentativas=5):
    for _ in range(tentativas):
        r = requests.post(URL, headers=HEADERS, json=payload, timeout=120)
        if r.status_code not in (429, 503):
            r.raise_for_status()
            return r.json()
        time.sleep(int(r.headers.get("Retry-After", 5)))
    raise RuntimeError("serviço indisponível após várias tentativas")
```

```js
const URL = "https://llmserve-docker.up.railway.app/v1/chat/completions"
const HEADERS = {
  "Content-Type": "application/json",
  Authorization: "Bearer " + process.env.STACK_API_KEY,
}

async function chamar(payload, tentativas = 5) {
  for (let i = 0; i < tentativas; i++) {
    const r = await fetch(URL, {
      method: "POST",
      headers: HEADERS,
      body: JSON.stringify(payload),
    })
    if (r.status !== 429 && r.status !== 503) {
      if (!r.ok) throw new Error("HTTP " + r.status)
      return r.json()
    }
    const espera = Number(r.headers.get("Retry-After") ?? 5)
    await new Promise((ok) => setTimeout(ok, espera * 1000))
  }
  throw new Error("serviço indisponível após várias tentativas")
}
```

Como o primeiro `Retry-After` pode ser de ~60 segundos, use um timeout de request
generoso (120s nos exemplos) e não trate a espera como erro na sua UI.

## Outros erros

| Status | Causa provável |
|---|---|
| `401` | Chave ausente, inválida, revogada ou expirada |
| `404` | Caminho fora da lista de rotas suportadas |
| `400` | Corpo inválido, ou prompt maior que a janela disponível |

Em caso de `401` numa chave que você sabe ser válida, verifique se ela não foi revogada
no painel e se não há espaços extras ao redor do valor copiado.
