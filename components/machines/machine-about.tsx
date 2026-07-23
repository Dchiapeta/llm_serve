"use client"

import { CodeBlock } from "@/components/ui/code-block"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"

export function MachineAbout({
  gatewayUrl,
  modelName,
  maxModelLen,
}: {
  gatewayUrl: string | null
  modelName: string | null
  maxModelLen: number | null
}) {
  // Sempre o gateway, nunca o proxy do pod: o pod muda/pausa e o cliente não
  // pode saber disso — realocação e auto-wake só funcionam via gateway.
  // Fallback é a URL real de produção (Railway) — GATEWAY_URL pode não estar
  // setado no ambiente do painel, e um placeholder deixaria o snippet inútil.
  const url =
    gatewayUrl?.replace(/\/$/, "") ?? "https://llmserve-docker.up.railway.app"
  const model = modelName ?? "<modelo>"

  const curlOpenAI = `curl ${url}/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer <SUA_CHAVE_DE_ACESSO>" \\
  -d '{
    "model": "${model}",
    "max_tokens": 8000,
    "messages": [{"role": "user", "content": "oi"}]
  }'`

  const curlAnthropic = `curl ${url}/v1/messages \\
  -H "Content-Type: application/json" \\
  -H "x-api-key: <SUA_CHAVE_DE_ACESSO>" \\
  -H "anthropic-version: 2023-06-01" \\
  -d '{
    "model": "${model}",
    "max_tokens": 8000,
    "messages": [{"role": "user", "content": "oi"}]
  }'`

  const python = `# pip install requests
import os
import requests

r = requests.post(
    "${url}/v1/chat/completions",
    headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + os.environ["LLM_API_KEY"],
    },
    json={
        "model": "${model}",
        "max_tokens": 8000,
        "messages": [{"role": "user", "content": "oi"}],
    },
    timeout=120,
)
r.raise_for_status()
print(r.json()["choices"][0]["message"]["content"])`

  const javascript = `// fetch nativo: Node 18+, Deno, Bun, browser. Sem dependências.
const r = await fetch("${url}/v1/chat/completions", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    Authorization: "Bearer " + process.env.LLM_API_KEY,
  },
  body: JSON.stringify({
    model: "${model}",
    max_tokens: 8000,
    messages: [{ role: "user", content: "oi" }],
  }),
})
if (!r.ok) throw new Error("HTTP " + r.status)

const data = await r.json()
console.log(data.choices[0].message.content)`

  const php = `<?php
$ch = curl_init("${url}/v1/chat/completions");
curl_setopt_array($ch, [
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_POST => true,
    CURLOPT_TIMEOUT => 120,
    CURLOPT_HTTPHEADER => [
        "Content-Type: application/json",
        "Authorization: Bearer " . getenv("LLM_API_KEY"),
    ],
    CURLOPT_POSTFIELDS => json_encode([
        "model" => "${model}",
        "max_tokens" => 8000,
        "messages" => [["role" => "user", "content" => "oi"]],
    ]),
]);
$data = json_decode(curl_exec($ch), true);
curl_close($ch);

echo $data["choices"][0]["message"]["content"];`

  const go = `package main

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
		"model":      "${model}",
		"max_tokens": 8000,
		"messages": []map[string]string{
			{"role": "user", "content": "oi"},
		},
	})

	req, _ := http.NewRequest("POST", "${url}/v1/chat/completions", bytes.NewReader(body))
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
}`

  const java = `// Java 11+. HttpClient é stdlib; parsing de JSON não é —
// para ler o campo content use Jackson ou Gson.
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

String body = """
    {"model": "${model}",
     "max_tokens": 8000,
     "messages": [{"role": "user", "content": "oi"}]}
    """;

HttpRequest req = HttpRequest.newBuilder()
    .uri(URI.create("${url}/v1/chat/completions"))
    .header("Content-Type", "application/json")
    .header("Authorization", "Bearer " + System.getenv("LLM_API_KEY"))
    .timeout(Duration.ofSeconds(120))
    .POST(HttpRequest.BodyPublishers.ofString(body))
    .build();

HttpResponse<String> res = HttpClient.newHttpClient()
    .send(req, HttpResponse.BodyHandlers.ofString());

System.out.println(res.body()); // JSON: choices[0].message.content`

  const csharp = `// .NET 6+
using System.Net.Http.Json;
using System.Text.Json;

using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(120) };
http.DefaultRequestHeaders.Add(
    "Authorization", "Bearer " + Environment.GetEnvironmentVariable("LLM_API_KEY"));

var res = await http.PostAsJsonAsync("${url}/v1/chat/completions", new
{
    model = "${model}",
    max_tokens = 8000,
    messages = new[] { new { role = "user", content = "oi" } },
});
res.EnsureSuccessStatusCode();

var json = await res.Content.ReadFromJsonAsync<JsonElement>();
Console.WriteLine(json.GetProperty("choices")[0]
    .GetProperty("message").GetProperty("content").GetString());`

  // AUTO_COMPACT_WINDOW: quando o Claude Code compacta antes de estourar a
  // janela real do plano (sem isso ele assume 200k e só descobre o limite
  // quando o gateway recusa). DERIVADO do --max-model-len da máquina,
  // espelhando a conta do gateway (context_budget.py): a saída garantida
  // (8000 = MIN_MAX_TOKENS) e o CONTEXT_SAFETY_FACTOR (1.2) comem a janela
  // crua, então o input útil ≈ (janela − 8000 − 200) / 1.2 — NÃO a janela
  // cheia (ex.: 131072 → ~102000, não 126k, senão o gateway rejeita).
  // Fallback 50000 (assume ≥64k) quando a janela é desconhecida (template sem
  // --max-model-len / pod anterior à migration 0031).
  const autoCompactWindow = maxModelLen
    ? Math.floor((maxModelLen - 8000 - 200) / 1.2 / 1000) * 1000
    : 50000

  const claudeSnippet = `export ANTHROPIC_BASE_URL="${url}"
export ANTHROPIC_AUTH_TOKEN="<SUA_CHAVE_DE_ACESSO>"
export ANTHROPIC_API_KEY=""
export ANTHROPIC_MODEL="${model}"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$ANTHROPIC_MODEL"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$ANTHROPIC_MODEL"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$ANTHROPIC_MODEL"
export CLAUDE_CODE_AUTO_COMPACT_WINDOW=${autoCompactWindow}
claude`

  const codexSnippet = `model_provider = "llmserve"
model = "${model}"

[model_providers.llmserve]
name = "llmserve"
base_url = "${url}/v1"
env_key = "LLMSERVE_API_KEY"
wire_api = "responses"`

  return (
    <div className="flex flex-col gap-6">
      <dl className="grid gap-3 rounded-md border p-4 text-sm sm:grid-cols-[auto_1fr] sm:gap-x-6">
        <dt className="font-medium">Endpoint</dt>
        <dd className="font-mono text-xs break-all text-muted-foreground">
          POST {url}/v1/chat/completions
        </dd>
        <dt className="font-medium">Autenticação</dt>
        <dd className="text-muted-foreground">
          <code className="font-mono text-xs">
            Authorization: Bearer &lt;chave&gt;
          </code>{" "}
          — a rota Anthropic também aceita{" "}
          <code className="font-mono text-xs">x-api-key</code>
        </dd>
        <dt className="font-medium">Resposta</dt>
        <dd className="text-muted-foreground">
          O texto vem em{" "}
          <code className="font-mono text-xs">
            choices[0].message.content
          </code>
        </dd>
        <dt className="font-medium">Campo model</dt>
        <dd className="text-muted-foreground">
          Livre. O gateway reescreve para o modelo do plano em toda request — o
          cliente não precisa acertar o nome.
        </dd>
        <dt className="font-medium">max_tokens</dt>
        <dd className="text-muted-foreground">
          Piso de 8000 e teto de 16000, aplicados pelo gateway.
        </dd>
      </dl>

      <Tabs defaultValue="terminal">
        <TabsList variant="line">
          <TabsTrigger value="terminal">Terminal</TabsTrigger>
          <TabsTrigger value="python">Python</TabsTrigger>
          <TabsTrigger value="js">JS / TS</TabsTrigger>
          <TabsTrigger value="php">PHP</TabsTrigger>
          <TabsTrigger value="outras">Outras</TabsTrigger>
          <TabsTrigger value="tools">Ferramentas</TabsTrigger>
        </TabsList>

        <TabsContent value="terminal" className="mt-4 flex flex-col gap-6">
          <div>
            <h3 className="mb-2 text-sm font-medium">curl — API OpenAI</h3>
            <CodeBlock code={curlOpenAI} />
          </div>
          <div>
            <h3 className="mb-2 text-sm font-medium">curl — API Anthropic</h3>
            <CodeBlock code={curlAnthropic} />
          </div>
        </TabsContent>

        <TabsContent value="python" className="mt-4">
          <CodeBlock code={python} />
        </TabsContent>

        <TabsContent value="js" className="mt-4">
          <CodeBlock code={javascript} />
        </TabsContent>

        <TabsContent value="php" className="mt-4">
          <CodeBlock code={php} />
        </TabsContent>

        <TabsContent value="outras" className="mt-4 flex flex-col gap-6">
          <div>
            <h3 className="mb-2 text-sm font-medium">Go</h3>
            <CodeBlock code={go} />
          </div>
          <div>
            <h3 className="mb-2 text-sm font-medium">Java</h3>
            <CodeBlock code={java} />
          </div>
          <div>
            <h3 className="mb-2 text-sm font-medium">C#</h3>
            <CodeBlock code={csharp} />
          </div>
        </TabsContent>

        <TabsContent value="tools" className="mt-4 flex flex-col gap-6">
          <div>
            <h3 className="mb-2 text-sm font-medium">Claude Code CLI</h3>
            <CodeBlock code={claudeSnippet} />
          </div>
          <div>
            <h3 className="mb-2 text-sm font-medium">Codex CLI</h3>
            <CodeBlock code={codexSnippet} label="~/.codex/config.toml" />
            <p className="mt-2 text-xs text-muted-foreground">
              A chave vai em <code className="font-mono">LLMSERVE_API_KEY</code>{" "}
              no ambiente.
            </p>
          </div>
          <div>
            <h3 className="mb-2 text-sm font-medium">
              Cursor, Cline, Continue e afins
            </h3>
            <p className="text-sm text-muted-foreground">
              Qualquer ferramenta com provider “OpenAI compatible”: aponte a base
              URL para{" "}
              <code className="font-mono text-xs break-all">{url}/v1</code>, use
              a chave de acesso como API key e coloque qualquer valor no campo de
              modelo.
            </p>
          </div>
        </TabsContent>
      </Tabs>

      <p className="text-xs text-muted-foreground">
        Máquina pausada responde <code className="font-mono">503</code> com{" "}
        <code className="font-mono">Retry-After</code> enquanto religa — o código
        do cliente precisa repetir a chamada. Guia completo em{" "}
        <code className="font-mono">docs/integracao.md</code>.
      </p>
    </div>
  )
}
