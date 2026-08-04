"use client"

import type { ReactNode } from "react"

import { CodeBlock } from "@/components/ui/code-block"

const GATEWAY_URL = "https://llmserve-docker.up.railway.app"

function Lead({ children }: { children: ReactNode }) {
  return <p className="text-foreground">{children}</p>
}

function Field({
  name,
  required,
  children,
}: {
  name: string
  required: boolean
  children: ReactNode
}) {
  return (
    <li>
      <code className="font-mono text-xs">{name}</code>{" "}
      <span className="text-xs">
        ({required ? "obrigatório" : "opcional"})
      </span>{" "}
      — {children}
    </li>
  )
}

export function ApiReference() {
  const mensagemBody = `{
  "model": "vibecoder-base",
  "max_tokens": 8000,
  "messages": [
    { "role": "user", "content": "oi" }
  ]
}`

  const imagemBody = `{
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
}`

  const documentoCurl = `curl -X POST ${GATEWAY_URL}/v1/documents/extract \\
  -H "Authorization: Bearer $STACK_API_KEY" \\
  -F file=@nota_fiscal.pdf \\
  -F 'schema={
        "type": "object",
        "properties": {
          "numero_nota":   {"type": "string"},
          "cnpj_emitente": {"type": ["string", "null"]},
          "valor_total":   {"type": "number"}
        },
        "required": ["numero_nota", "cnpj_emitente", "valor_total"]
      }'`

  const documentoResposta = `{
  "data": { "numero_nota": "12345", "cnpj_emitente": "11.222.333/0001-44", "valor_total": 1500.0 },
  "pages": 3,
  "ocr_used": false,
  "usage": { "prompt_tokens": 2104, "completion_tokens": 48 }
}`

  return (
    <div className="flex flex-col gap-8">
      <Lead>
        A request que precisa ser feita para cada caso de uso do serviço. Todas
        usam a mesma autenticação —{" "}
        <code className="font-mono text-xs">
          Authorization: Bearer &lt;chave&gt;
        </code>{" "}
        — contra <code className="font-mono text-xs">{GATEWAY_URL}</code>.
      </Lead>

      {/* Mensagem */}
      <div className="flex flex-col gap-3">
        <h3 className="text-sm font-medium text-foreground">
          1. Mensagem (chat)
        </h3>
        <p>
          <code className="font-mono text-xs">
            POST /v1/chat/completions
          </code>{" "}
          — conversa de texto simples, formato compatível com a OpenAI.
        </p>
        <CodeBlock code={mensagemBody} />
        <ul className="list-disc space-y-1.5 pl-5 marker:text-muted-foreground">
          <Field name="messages" required>
            lista de mensagens (<code className="font-mono text-xs">role</code>
            : <code className="font-mono text-xs">system</code>,{" "}
            <code className="font-mono text-xs">user</code>,{" "}
            <code className="font-mono text-xs">assistant</code> ou{" "}
            <code className="font-mono text-xs">tool</code>)
          </Field>
          <Field name="model" required={false}>
            ignorado — o serviço sempre usa o modelo do seu plano
          </Field>
          <Field name="max_tokens" required={false}>
            piso de 8000, teto de 16000
          </Field>
          <Field name="stream" required={false}>
            <code className="font-mono text-xs">true</code> para receber a
            resposta em SSE, token a token
          </Field>
        </ul>
        <p>
          Resposta: texto em{" "}
          <code className="font-mono text-xs">
            choices[0].message.content
          </code>
          . Formato Anthropic equivalente:{" "}
          <code className="font-mono text-xs">POST /v1/messages</code> com
          header <code className="font-mono text-xs">x-api-key</code> em vez
          de <code className="font-mono text-xs">Authorization</code>.
        </p>
      </div>

      {/* Imagem OCR */}
      <div className="flex flex-col gap-3">
        <h3 className="text-sm font-medium text-foreground">
          2. Imagem — OCR / leitura de imagem
        </h3>
        <p>
          Não é um endpoint separado: é a{" "}
          <span className="font-medium text-foreground">
            mesma rota de chat
          </span>
          , enviando a imagem como uma parte{" "}
          <code className="font-mono text-xs">image_url</code> dentro do{" "}
          <code className="font-mono text-xs">content</code> da mensagem,
          junto com a instrução em texto.
        </p>
        <CodeBlock code={imagemBody} />
        <ul className="list-disc space-y-1.5 pl-5 marker:text-muted-foreground">
          <Field name={'content[].type = "image_url"'} required>
            marca a parte como imagem (uma por imagem enviada)
          </Field>
          <Field name="image_url.url" required>
            data URL <code className="font-mono text-xs">
              data:&lt;mime&gt;;base64,&lt;dados&gt;
            </code>{" "}
            — a imagem viaja no próprio corpo, sem upload prévio
          </Field>
          <Field name={'content[].type = "text"'} required={false}>
            recomendado — a instrução do que fazer com a imagem (transcrever,
            descrever, comparar)
          </Field>
        </ul>
        <p>
          Resposta: mesma forma do chat comum. Corpo total (JSON + imagem em
          base64) até <span className="font-medium text-foreground">8 MB</span>
          ; imagens além do teto do plano são recortadas automaticamente (o
          modelo é avisado em texto) em vez de gerar erro.
        </p>
      </div>

      {/* Documento OCR */}
      <div className="flex flex-col gap-3">
        <h3 className="text-sm font-medium text-foreground">
          3. Documento — OCR / extração estruturada (PDF → JSON)
        </h3>
        <p>
          Endpoint dedicado:{" "}
          <code className="font-mono text-xs">
            POST /v1/documents/extract
          </code>{" "}
          (<code className="font-mono text-xs">multipart/form-data</code>).
          Recebe um PDF — inclusive escaneado, faz OCR internamente — e
          devolve um JSON validado contra o schema que você define.
        </p>
        <ul className="list-disc space-y-1.5 pl-5 marker:text-muted-foreground">
          <Field name="file" required>
            o PDF
          </Field>
          <Field name="schema" required>
            JSON Schema (como string) descrevendo os campos a extrair
          </Field>
          <Field name="max_tokens" required={false}>
            teto da resposta — default 4000, máximo 16000
          </Field>
        </ul>
        <CodeBlock code={documentoCurl} />
        <h4 className="text-xs font-medium text-foreground">Resposta</h4>
        <CodeBlock code={documentoResposta} />
        <p>
          <code className="font-mono text-xs">data</code>: seu JSON já
          validado contra o schema.{" "}
          <code className="font-mono text-xs">ocr_used</code>:{" "}
          <code className="font-mono text-xs">true</code> se alguma página
          precisou de OCR — vale conferir o resultado com mais atenção nesse
          caso.
        </p>
        <p>
          <span className="font-medium text-foreground">
            Regra de schema mais importante:
          </span>{" "}
          todo campo em <code className="font-mono text-xs">required</code> e
          anulável (<code className="font-mono text-xs">
            {'{"type": ["string", "null"]}'}
          </code>
          ) quando puder legitimamente faltar no documento — do contrário o
          modelo é forçado a inventar um valor (campo obrigatório e
          não-anulável) ou pode omitir o campo em silêncio (campo fora de{" "}
          <code className="font-mono text-xs">required</code>).
        </p>

        <div className="overflow-x-auto rounded-md border">
          <table className="w-full text-left text-xs">
            <thead className="border-b bg-muted/50 text-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">Limite</th>
                <th className="px-3 py-2 font-medium">VibeCoder</th>
                <th className="px-3 py-2 font-medium">Pro</th>
              </tr>
            </thead>
            <tbody>
              <tr className="border-b">
                <td className="px-3 py-2">Tamanho do arquivo</td>
                <td className="px-3 py-2">8 MB</td>
                <td className="px-3 py-2">15 MB</td>
              </tr>
              <tr>
                <td className="px-3 py-2">Páginas por requisição</td>
                <td className="px-3 py-2">15</td>
                <td className="px-3 py-2">30</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p>
          Schema até 64 KB. Timeout do servidor: 240s — dimensione o timeout do
          seu cliente acima disso, documentos escaneados com várias páginas
          podem levar minutos.
        </p>

        <div className="overflow-x-auto rounded-md border">
          <table className="w-full text-left text-xs">
            <thead className="border-b bg-muted/50 text-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Significado</th>
              </tr>
            </thead>
            <tbody>
              <tr className="border-b">
                <td className="px-3 py-2 font-mono">400</td>
                <td className="px-3 py-2">
                  PDF ilegível/corrompido, sem texto extraível, schema
                  inválido, ou documento grande demais para a janela do plano
                </td>
              </tr>
              <tr className="border-b">
                <td className="px-3 py-2 font-mono">413</td>
                <td className="px-3 py-2">
                  Arquivo, número de páginas ou schema acima do limite
                </td>
              </tr>
              <tr className="border-b">
                <td className="px-3 py-2 font-mono">422</td>
                <td className="px-3 py-2">
                  <code className="font-mono text-xs">max_tokens</code> fora
                  da faixa aceita
                </td>
              </tr>
              <tr>
                <td className="px-3 py-2 font-mono">502</td>
                <td className="px-3 py-2">
                  Modelo não devolveu JSON aderente ao schema (resposta inclui{" "}
                  <code className="font-mono text-xs">raw_output</code> para
                  diagnóstico)
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <p className="text-xs text-muted-foreground">
        Erros comuns aos três casos —{" "}
        <code className="font-mono">401</code> (chave ausente/inválida/
        revogada), <code className="font-mono">404</code> (rota inexistente),{" "}
        <code className="font-mono">429</code>/<code className="font-mono">503</code>{" "}
        (rate limit, cota ou infraestrutura religando) — seguem as mesmas
        regras da aba &ldquo;Exemplos de chamada&rdquo; e de{" "}
        <code className="font-mono">docs/integracao.md</code>.
      </p>
    </div>
  )
}
