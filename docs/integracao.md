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
  "model": "vibecoder-base",
  "max_tokens": 8000,
  "messages": [{"role": "user", "content": "oi"}]
}
```

O campo `model` é **livre**: o serviço sempre usa o modelo do seu plano, seja qual for
o valor enviado. Ele é obrigatório apenas porque o formato o exige — você não precisa
descobrir nem acertar o nome. Se quiser o nome real, consulte `GET /v1/models`.

### Outras rotas

Além de `/v1/chat/completions`, o serviço aceita `/v1/completions`, `/v1/embeddings`,
`/v1/responses` e `GET /v1/models` (formato OpenAI), e `/v1/messages` e
`/v1/messages/count_tokens` (formato Anthropic). Qualquer outro caminho responde `404`.

---

## Exemplos

Todos os exemplos abaixo fazem a mesma chamada e leem a chave da variável de ambiente
`LLM_API_KEY`.

### curl

```bash
curl https://llmserve-docker.up.railway.app/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LLM_API_KEY" \
  -d '{
    "model": "vibecoder-base",
    "max_tokens": 8000,
    "messages": [{"role": "user", "content": "oi"}]
  }'
```

No formato Anthropic, se preferir:

```bash
curl https://llmserve-docker.up.railway.app/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: $LLM_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "vibecoder-base",
    "max_tokens": 8000,
    "messages": [{"role": "user", "content": "oi"}]
  }'
```

### Python

```python
# pip install requests
import os
import requests

r = requests.post(
    "https://llmserve-docker.up.railway.app/v1/chat/completions",
    headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + os.environ["LLM_API_KEY"],
    },
    json={
        "model": "vibecoder-base",
        "max_tokens": 8000,
        "messages": [{"role": "user", "content": "oi"}],
    },
    timeout=120,
)
r.raise_for_status()
print(r.json()["choices"][0]["message"]["content"])
```

`requests` não faz parte da biblioteca padrão. Para evitar a dependência, dá para usar
`urllib.request` com `json.dumps()` no corpo — a request é a mesma.

### JavaScript / TypeScript

```js
// fetch nativo: Node 18+, Deno, Bun, browser. Sem dependências.
const r = await fetch("https://llmserve-docker.up.railway.app/v1/chat/completions", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    Authorization: "Bearer " + process.env.LLM_API_KEY,
  },
  body: JSON.stringify({
    model: "vibecoder-base",
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
        "Authorization: Bearer " . getenv("LLM_API_KEY"),
    ],
    CURLOPT_POSTFIELDS => json_encode([
        "model" => "vibecoder-base",
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
		"model":      "vibecoder-base",
		"max_tokens": 8000,
		"messages": []map[string]string{
			{"role": "user", "content": "oi"},
		},
	})

	req, _ := http.NewRequest("POST",
		"https://llmserve-docker.up.railway.app/v1/chat/completions",
		bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+os.Getenv("LLM_API_KEY"))

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
    {"model": "vibecoder-base",
     "max_tokens": 8000,
     "messages": [{"role": "user", "content": "oi"}]}
    """;

HttpRequest req = HttpRequest.newBuilder()
    .uri(URI.create("https://llmserve-docker.up.railway.app/v1/chat/completions"))
    .header("Content-Type", "application/json")
    .header("Authorization", "Bearer " + System.getenv("LLM_API_KEY"))
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
    "Authorization", "Bearer " + Environment.GetEnvironmentVariable("LLM_API_KEY"));

var res = await http.PostAsJsonAsync(
    "https://llmserve-docker.up.railway.app/v1/chat/completions", new
{
    model = "vibecoder-base",
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

r = requests.post(
    "https://llmserve-docker.up.railway.app/v1/chat/completions",
    headers={"Authorization": "Bearer " + os.environ["LLM_API_KEY"]},
    json={
        "model": "vibecoder-base",
        "max_tokens": 8000,
        "stream": True,
        "messages": [{"role": "user", "content": "escreva um haiku"}],
    },
    stream=True,
    timeout=120,
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
export ANTHROPIC_MODEL="vibecoder-base"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$ANTHROPIC_MODEL"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$ANTHROPIC_MODEL"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$ANTHROPIC_MODEL"
export CLAUDE_CODE_AUTO_COMPACT_WINDOW=50000
claude
```

`CLAUDE_CODE_AUTO_COMPACT_WINDOW` não é opcional: o Claude Code assume uma janela de
200k e não tem como descobrir a real do seu plano. Sem essa variável ele só percebe o
limite quando a chamada é recusada; com ela, compacta a conversa antes de estourar.

### Codex CLI

Em `~/.codex/config.toml`:

```toml
model_provider = "llmserve"
model = "vibecoder-base"

[model_providers.llmserve]
name = "llmserve"
base_url = "https://llmserve-docker.up.railway.app/v1"
env_key = "LLMSERVE_API_KEY"
wire_api = "responses"
```

A chave vai na variável de ambiente `LLMSERVE_API_KEY`.

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

---

## Retry: o ponto mais importante para produção

A infraestrutura do seu plano pode estar pausada por inatividade. A primeira chamada
depois de um período parado **religa a máquina e responde `503`**, com o header
`Retry-After` dizendo quantos segundos esperar. É o funcionamento normal, não uma falha.

| Status | `Retry-After` | Significado |
|---|---|---|
| `503` | ~60s | Infraestrutura religando após pausa por inatividade |
| `503` | ~5s | Preparo de recursos do seu plano em andamento |
| `429` | variável | Limite de requisições por minuto atingido |
| `429` | 3600s | Cota diária de tokens do plano esgotada |

Como você está chamando a API diretamente, **o retry é responsabilidade do seu código**.
Sem ele, a primeira chamada do dia falha na cara do seu usuário. O padrão é simples:
repetir enquanto o status for `429` ou `503`, dormindo o que o `Retry-After` mandar.

```python
import os, time, requests

URL = "https://llmserve-docker.up.railway.app/v1/chat/completions"
HEADERS = {
    "Content-Type": "application/json",
    "Authorization": "Bearer " + os.environ["LLM_API_KEY"],
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
  Authorization: "Bearer " + process.env.LLM_API_KEY,
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
