"use client"

import { CodeBlock } from "@/components/ui/code-block"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { autoCompactWindow } from "@/lib/context-window"
import {
  CLI_BLOCKED_PLANS,
  type ProductCategory,
  type TemplatePlan,
} from "@/lib/types"

export function MachineAbout({
  gatewayUrl,
  modelName,
  maxModelLen,
  plan,
  category = null,
}: {
  gatewayUrl: string | null
  modelName: string | null
  maxModelLen: number | null
  // Plano do template da máquina. null = máquina sem template (não dá pra
  // saber o plano) — mostra as ferramentas, mesmo fail-open do gateway quando
  // o plano não é resolvível.
  plan: TemplatePlan | null
  // Categoria de workload do template. Uma máquina de difusão não fala
  // /v1/chat/completions — o gateway responde 403 —, então ensinar os exemplos
  // de chat nela seria mandar o cliente montar algo que falha em toda request.
  // null cai no caminho de LLM, mesmo fail-open de `plan`.
  category?: ProductCategory | null
}) {
  // Sempre o gateway, nunca o proxy do pod: o pod muda/pausa e o cliente não
  // pode saber disso — realocação e auto-wake só funcionam via gateway.
  // Fallback é a URL real de produção (Railway) — GATEWAY_URL pode não estar
  // setado no ambiente do painel, e um placeholder deixaria o snippet inútil.
  const url =
    gatewayUrl?.replace(/\/$/, "") ?? "https://api.trystac.com"

  if (category === "image") return <ImageAbout url={url} />

  return <LlmAbout url={url} modelName={modelName} maxModelLen={maxModelLen} plan={plan} />
}

/** Exemplos das rotas de difusão. Ver components/documentacao/api-reference.tsx
 *  (seção 4) e content/docs/*\/api-reference/image.mdx no TryStac — os três
 *  precisam contar a mesma história. */
function ImageAbout({ url }: { url: string }) {
  const curlGeracao = `curl -X POST ${url}/v1/images/generations \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer <SUA_CHAVE_DE_ACESSO>" \\
  -d '{
    "prompt": "uma camisa social branca dobrada sobre uma mesa de madeira",
    "size": "1024x1024",
    "seed": 42
  }'`

  // Sem "model" de propósito: nesta rota o gateway não aplica pin_model, e um
  // nome diferente do servido devolve 404 do pod.
  const curlEdicao = `curl -X POST ${url}/v1/images/edits \\
  -H "Authorization: Bearer <SUA_CHAVE_DE_ACESSO>" \\
  -F "prompt=deixe em preto e branco" \\
  -F "size=1024x1024" \\
  -F "image[]=@foto.png"`

  const python = `# pip install requests
import base64
import os

import requests

r = requests.post(
    "${url}/v1/images/generations",
    headers={"Authorization": f"Bearer {os.environ['STACK_API_KEY']}"},
    json={"prompt": "um gato astronauta", "size": "1024x1024"},
    timeout=180,
)
r.raise_for_status()
payload = r.json()

with open("saida.png", "wb") as f:
    f.write(base64.b64decode(payload["data"][0]["b64_json"]))

print(payload["meta"]["seed"], payload["meta"]["timings"]["gpu_s"])`

  const javascript = `import { writeFile } from "node:fs/promises"

const r = await fetch("${url}/v1/images/generations", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    Authorization: \`Bearer \${process.env.STACK_API_KEY}\`,
  },
  body: JSON.stringify({ prompt: "um gato astronauta", size: "1024x1024" }),
})

const payload = await r.json()
await writeFile("saida.png", Buffer.from(payload.data[0].b64_json, "base64"))`

  return (
    <div className="flex flex-col gap-6">
      <dl className="grid gap-3 rounded-md border p-4 text-sm sm:grid-cols-[auto_1fr] sm:gap-x-6">
        <dt className="font-medium">Endpoints</dt>
        <dd className="font-mono text-xs break-all text-muted-foreground">
          POST {url}/v1/images/generations
          <br />
          POST {url}/v1/images/edits
        </dd>
        <dt className="font-medium">Autenticação</dt>
        <dd className="text-muted-foreground">
          <code className="font-mono text-xs">
            Authorization: Bearer &lt;chave&gt;
          </code>{" "}
          — <code className="font-mono text-xs">x-api-key</code> não funciona
          nestas rotas
        </dd>
        <dt className="font-medium">Resposta</dt>
        <dd className="text-muted-foreground">
          Sempre <code className="font-mono text-xs">b64_json</code>, em{" "}
          <code className="font-mono text-xs">data[0].b64_json</code>.{" "}
          <code className="font-mono text-xs">response_format: &quot;url&quot;</code>{" "}
          é recusado.
        </dd>
        <dt className="font-medium">Campo model</dt>
        <dd className="text-muted-foreground">
          Livre em <code className="font-mono text-xs">generations</code> (o
          gateway reescreve). Em{" "}
          <code className="font-mono text-xs">edits</code>{" "}
          <span className="font-medium text-foreground">não é reescrito</span> —
          omita, ou o pod devolve{" "}
          <code className="font-mono text-xs">404</code>.
        </dd>
        <dt className="font-medium">Ritmo</dt>
        <dd className="text-muted-foreground">
          10 submissões/min por stack, 3 em voo (1 gerando + 2 na fila), 60s de
          espera máxima. Sem cota de tokens.
        </dd>
        <dt className="font-medium">Prompt</dt>
        <dd className="text-muted-foreground">
          Truncado em 512 tokens,{" "}
          <span className="font-medium text-foreground">sem aviso</span>.
        </dd>
      </dl>

      <Tabs defaultValue="terminal">
        <TabsList variant="line">
          <TabsTrigger value="terminal">Terminal</TabsTrigger>
          <TabsTrigger value="python">Python</TabsTrigger>
          <TabsTrigger value="js">JS / TS</TabsTrigger>
        </TabsList>

        <TabsContent value="terminal" className="mt-4 flex flex-col gap-6">
          <div>
            <h3 className="mb-2 text-sm font-medium">curl — texto → imagem</h3>
            <CodeBlock code={curlGeracao} />
          </div>
          <div>
            <h3 className="mb-2 text-sm font-medium">
              curl — edição (até 4 referências de 5 MiB, PNG/JPEG/WEBP)
            </h3>
            <CodeBlock code={curlEdicao} />
          </div>
        </TabsContent>

        <TabsContent value="python" className="mt-4">
          <CodeBlock code={python} />
        </TabsContent>

        <TabsContent value="js" className="mt-4">
          <CodeBlock code={javascript} />
        </TabsContent>
      </Tabs>

      <p className="text-xs text-muted-foreground">
        Esta máquina serve difusão, não chat: as rotas de LLM respondem{" "}
        <code className="font-mono">403</code> com uma chave desta stack. Toda
        imagem gerada é armazenada por 30 dias. Detalhes completos na aba
        Documentação.
      </p>
    </div>
  )
}

function LlmAbout({
  url,
  modelName,
  maxModelLen,
  plan,
}: {
  url: string
  modelName: string | null
  maxModelLen: number | null
  plan: TemplatePlan | null
}) {
  // Planos sem CLI não podem ver a config de Claude Code/Codex: o gateway
  // responde 403 nessas rotas (docker/gateway/cli_policy.py), então ensiná-la
  // aqui seria mandar o cliente montar algo que não funciona.
  const cliAllowed = !plan || !CLI_BLOCKED_PLANS.includes(plan)
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

message = "content here"

r = requests.post(
    "${url}/v1/chat/completions",
    headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + os.environ["STACK_API_KEY"],
    },
    json={"messages": [{"role": "user", "content": message}]},
)
r.raise_for_status()
print(r.json()["choices"][0]["message"]["content"])`

  const javascript = `// fetch nativo: Node 18+, Deno, Bun, browser. Sem dependências.
const r = await fetch("${url}/v1/chat/completions", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    Authorization: "Bearer " + process.env.STACK_API_KEY,
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
        "Authorization: Bearer " . getenv("STACK_API_KEY"),
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
    .header("Authorization", "Bearer " + System.getenv("STACK_API_KEY"))
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
    "Authorization", "Bearer " + Environment.GetEnvironmentVariable("STACK_API_KEY"));

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

  // AUTO_COMPACT_WINDOW: a janela real do plano menos a saída garantida e uma
  // margem para o transbordo de um turno — é o que faz o Claude Code compactar
  // antes de estourar. Sem isso ele assume 200k (não há como anunciar a janela
  // real pela API: ele ignora /v1/models e só tem o env var) e só descobre o
  // limite quando o gateway recusa. Conta centralizada em
  // lib/context-window.ts, espelho de docker/gateway/context_budget.py.
  const compactWindow = autoCompactWindow(maxModelLen)

  const claudeSnippet = `export ANTHROPIC_BASE_URL="${url}"
export ANTHROPIC_AUTH_TOKEN="<SUA_CHAVE_DE_ACESSO>"
export ANTHROPIC_API_KEY=""
export ANTHROPIC_MODEL="${model}"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$ANTHROPIC_MODEL"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$ANTHROPIC_MODEL"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$ANTHROPIC_MODEL"
export CLAUDE_CODE_AUTO_COMPACT_WINDOW=${compactWindow}
claude`

  // Mesma config, persistente: o export de shell vale só pra sessão em que foi
  // rodado, e esquecê-lo é o caminho para a sessão travar em 400 de contexto.
  // Note que aqui os valores são STRINGS e não há interpolação — o nome do
  // modelo é repetido literalmente, "$ANTHROPIC_MODEL" não funciona em JSON.
  const claudeSettings = `{
  "env": {
    "ANTHROPIC_BASE_URL": "${url}",
    "ANTHROPIC_AUTH_TOKEN": "<SUA_CHAVE_DE_ACESSO>",
    "ANTHROPIC_API_KEY": "",
    "ANTHROPIC_MODEL": "${model}",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "${model}",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "${model}",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "${model}",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "${compactWindow}"
  }
}`

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
          {cliAllowed && <TabsTrigger value="tools">Ferramentas</TabsTrigger>}
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

        {cliAllowed && (
        <TabsContent value="tools" className="mt-4 flex flex-col gap-6">
          <div>
            <h3 className="mb-2 text-sm font-medium">Claude Code CLI</h3>
            <CodeBlock code={claudeSnippet} />
            <p className="mt-2 text-xs text-muted-foreground">
              O{" "}
              <code className="font-mono">
                CLAUDE_CODE_AUTO_COMPACT_WINDOW
              </code>{" "}
              é o que faz o Claude Code compactar a conversa sozinho, antes de
              encher a janela do plano
              {maxModelLen ? ` (${maxModelLen.toLocaleString("pt-BR")} tokens)` : ""}
              . Sem ele o Claude Code assume 200 mil tokens e a sessão trava com
              erro de contexto antes de compactar. O valor é a capacidade que ele
              passa a assumir, não o ponto exato da compactação — ele compacta um
              pouco antes; o resto da janela fica reservado para a resposta e para
              absorver um anexo grande no turno seguinte.
            </p>
          </div>
          <div>
            <h3 className="mb-2 text-sm font-medium">
              Claude Code CLI — configuração permanente
            </h3>
            <CodeBlock code={claudeSettings} label="~/.claude/settings.json" />
            <p className="mt-2 text-xs text-muted-foreground">
              Os <code className="font-mono">export</code> acima valem só para a
              sessão do terminal em que foram rodados; este arquivo vale para
              todas. Use o{" "}
              <code className="font-mono">~/.claude/settings.json</code> da sua
              conta, não o{" "}
              <code className="font-mono">.claude/settings.json</code> do
              projeto — esse vai para o git e levaria a sua chave de acesso com
              ele.
            </p>
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
        )}
      </Tabs>

      {!cliAllowed && (
        <p className="text-xs text-muted-foreground">
          O plano {plan} não inclui uso via CLI ou assistente de código (Claude
          Code, Codex, Cursor, Cline) — o gateway responde{" "}
          <code className="font-mono">403</code> nessas rotas. É do Pro para
          cima.
        </p>
      )}

      <p className="text-xs text-muted-foreground">
        Máquina pausada responde <code className="font-mono">503</code> com{" "}
        <code className="font-mono">Retry-After</code> enquanto religa — o código
        do cliente precisa repetir a chamada. Guia completo em{" "}
        <code className="font-mono">docs/integracao.md</code>.
      </p>
    </div>
  )
}
