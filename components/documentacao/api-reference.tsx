"use client"

import type { ReactNode } from "react"

import { CodeBlock } from "@/components/ui/code-block"

const GATEWAY_URL = "https://api.trystac.com"

function Lead({ children }: { children: ReactNode }) {
  return <p className="text-foreground">{children}</p>
}

// `required` aceita uma string para o caso que não é nem um nem outro: o
// `prompt` das rotas de difusão, que é obrigatório no corpo só quando a chave
// não tem um configurado.
function Field({
  name,
  required,
  children,
}: {
  name: string
  required: boolean | string
  children: ReactNode
}) {
  const label =
    typeof required === "string"
      ? required
      : required
        ? "obrigatório"
        : "opcional"
  return (
    <li>
      <code className="font-mono text-xs">{name}</code>{" "}
      <span className="text-xs">({label})</span> — {children}
    </li>
  )
}

export function ApiReference() {
  const mensagemBody = `{
  "model": "go-base",
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
  "files": 1,
  "ocr_used": false,
  "usage": { "prompt_tokens": 2104, "completion_tokens": 48 }
}`

  const geracaoBody = `{
  "prompt": "uma camisa social branca dobrada sobre uma mesa de madeira",
  "size": "1024x1024",
  "steps": 4,
  "guidance_scale": 1.0,
  "seed": 42
}`

  // O curl de edição omite "model" de propósito: nesta rota o gateway NÃO
  // aplica pin_model (reescrever exigiria re-encodar o multipart), então um
  // nome errado ali vira 404 do pod.
  const edicaoCurl = `curl -X POST ${GATEWAY_URL}/v1/images/edits \\
  -H "Authorization: Bearer $STACK_API_KEY" \\
  -F "prompt=deixe em preto e branco" \\
  -F "size=1024x1024" \\
  -F "image[]=@foto.png"`

  // O caso que o prompt por chave existe para servir: o cliente manda url,
  // chave e imagem, e nada mais.
  const edicaoSemPromptCurl = `curl -X POST ${GATEWAY_URL}/v1/images/edits \\
  -H "Authorization: Bearer $STACK_API_KEY" \\
  -F "image[]=@foto.png"`

  const geracaoResposta = `{
  "created": 1767225600,
  "data": [{ "b64_json": "iVBORw0KGgo..." }],
  "meta": {
    "width": 1024, "height": 1024, "steps": 4,
    "guidance_scale": 1.0, "seed": 8151234567890123456, "n": 1,
    "model": "flux2-klein-4b",
    "timings": {
      "queue_wait_s": 0.0100, "decode_s": null, "gpu_s": 4.1000,
      "encode_s": 0.1800, "worker_s": 4.2800
    }
  }
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
        <p>
          <code className="font-mono text-xs">/v1/messages</code> e{" "}
          <code className="font-mono text-xs">/v1/responses</code> são as rotas
          do Claude Code e do Codex, e exigem plano{" "}
          <span className="font-medium text-foreground">Pro ou superior</span> —
          no Go respondem <code className="font-mono text-xs">403</code>.{" "}
          <code className="font-mono text-xs">/v1/chat/completions</code>{" "}
          funciona em todos os planos.
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
          devolve um JSON validado contra o schema que você define. A partir
          do Pro, aceita vários PDFs na mesma requisição (campo{" "}
          <code className="font-mono text-xs">files</code>) e devolve um JSON
          só, com cada arquivo num bloco numerado para o modelo.
        </p>
        <ul className="list-disc space-y-1.5 pl-5 marker:text-muted-foreground">
          <Field name="file" required>
            o PDF (ou vários em{" "}
            <code className="font-mono text-xs">files</code>, repetindo o
            campo — a partir do Pro; no Go um segundo arquivo devolve 413, não
            é descartado em silêncio)
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
          <code className="font-mono text-xs">pages</code> e{" "}
          <code className="font-mono text-xs">files</code>: páginas (somadas)
          e arquivos que entraram na extração.{" "}
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
                <th className="px-3 py-2 font-medium">Go</th>
                <th className="px-3 py-2 font-medium">Pro</th>
                <th className="px-3 py-2 font-medium">Max</th>
              </tr>
            </thead>
            <tbody>
              <tr className="border-b">
                <td className="px-3 py-2">Arquivos por requisição</td>
                <td className="px-3 py-2">1</td>
                <td className="px-3 py-2">5</td>
                <td className="px-3 py-2">10</td>
              </tr>
              <tr className="border-b">
                <td className="px-3 py-2">Tamanho (soma dos arquivos)</td>
                <td className="px-3 py-2">8 MB</td>
                <td className="px-3 py-2">15 MB</td>
                <td className="px-3 py-2">25 MB</td>
              </tr>
              <tr>
                <td className="px-3 py-2">Páginas (soma dos arquivos)</td>
                <td className="px-3 py-2">15</td>
                <td className="px-3 py-2">30</td>
                <td className="px-3 py-2">50</td>
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
                  Arquivo, número de arquivos, número de páginas ou schema
                  acima do limite do plano
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

      {/* Geração de imagem — difusão, não é o /v1/images/extract da seção 2 */}
      <div className="flex flex-col gap-3">
        <h3 className="text-sm font-medium text-foreground">
          4. Imagem — geração e edição
        </h3>
        <p>
          Duas rotas de difusão:{" "}
          <code className="font-mono text-xs">
            POST /v1/images/generations
          </code>{" "}
          (JSON) cria uma imagem a partir de um prompt, e{" "}
          <code className="font-mono text-xs">POST /v1/images/edits</code> (
          <code className="font-mono text-xs">multipart/form-data</code>) edita
          até 4 imagens de referência. A resposta é sempre{" "}
          <code className="font-mono text-xs">b64_json</code>.
        </p>
        <p>
          <span className="font-medium text-foreground">
            Não confundir com a seção 2:
          </span>{" "}
          <code className="font-mono text-xs">/v1/images/extract</code> é OCR
          por LLM multimodal (imagem → JSON). Estas rotas geram pixels, rodam
          num pod dedicado e exigem uma chave de stack da categoria{" "}
          <code className="font-mono text-xs">image</code> — uma chave de LLM
          recebe <code className="font-mono text-xs">403</code>, e vice-versa.
        </p>
        <CodeBlock code={geracaoBody} />
        <ul className="list-disc space-y-1.5 pl-5 marker:text-muted-foreground">
          <Field name="prompt" required="condicional">
            texto livre. Pode ser omitido se a chave (ou a stack) tiver um
            prompt configurado — ver abaixo. Em ambos os casos é{" "}
            <span className="font-medium text-foreground">
              truncado em 512 tokens, sem aviso nem erro
            </span>
          </Field>
          <Field name="size" required={false}>
            lista fechada:{" "}
            <code className="font-mono text-xs">1024x1024</code> (padrão),{" "}
            <code className="font-mono text-xs">1536x1024</code>,{" "}
            <code className="font-mono text-xs">1024x1536</code>
          </Field>
          <Field name="steps" required={false}>
            padrão 4, teto 8 (o checkpoint é <i>distilled</i>)
          </Field>
          <Field name="guidance_scale" required={false}>
            0 a 20, padrão 1.0
          </Field>
          <Field name="seed" required={false}>
            0 a 2^64−1. Omitida, o servidor sorteia e devolve em{" "}
            <code className="font-mono text-xs">meta.seed</code> — é do lote,
            não da imagem
          </Field>
          <Field name="n" required={false}>
            hoje só aceita <code className="font-mono text-xs">1</code>
          </Field>
          <Field name="model" required={false}>
            ignorado em{" "}
            <code className="font-mono text-xs">generations</code>; em{" "}
            <code className="font-mono text-xs">edits</code>{" "}
            <span className="font-medium text-foreground">não é corrigido</span>{" "}
            — omita, ou <code className="font-mono text-xs">404</code>
          </Field>
        </ul>
        <h4 className="text-xs font-medium text-foreground">
          Edição (multipart)
        </h4>
        <CodeBlock code={edicaoCurl} />
        <p>
          Campo <code className="font-mono text-xs">image</code> ou{" "}
          <code className="font-mono text-xs">image[]</code>, de 1 a 4 arquivos
          de 5 MiB cada, PNG/JPEG/WEBP detectados por magic bytes. Corpo total
          até 21 MiB (contra 256 KiB em{" "}
          <code className="font-mono text-xs">generations</code>).{" "}
          <code className="font-mono text-xs">mask</code> devolve{" "}
          <code className="font-mono text-xs">400</code>.
        </p>
        <h4 className="text-xs font-medium text-foreground">
          Prompt configurado na chave
        </h4>
        <p>
          O <code className="font-mono text-xs">prompt</code> pode viver na
          chave de API, como o system prompt das rotas de texto, e vale a mesma
          precedência: o{" "}
          <code className="font-mono text-xs">prompt</code> do request ganha;
          sem ele (ou em branco) vale o da chave; sem o da chave, o da stack. É
          o que permite ao cliente mandar só URL, chave e imagem:
        </p>
        <CodeBlock code={edicaoSemPromptCurl} />
        <p>
          <span className="font-medium text-foreground">
            O prompt do request SUBSTITUI o da chave, não soma.
          </span>{" "}
          Não há concatenação: quem manda{" "}
          <code className="font-mono text-xs">prompt</code> descarta o
          configurado por inteiro. E o texto da chave gasta o mesmo orçamento de{" "}
          <span className="font-medium text-foreground">512 tokens</span> — um
          prompt longo na chave não sobra espaço para mais nada, e o corte
          continua silencioso.
        </p>
        <p>
          Sem prompt em lugar nenhum a resposta é{" "}
          <code className="font-mono text-xs">400 missing_prompt</code>.
        </p>
        <h4 className="text-xs font-medium text-foreground">Resposta</h4>
        <CodeBlock code={geracaoResposta} />
        <p>
          <code className="font-mono text-xs">meta</code> traz os parâmetros
          efetivos.{" "}
          <code className="font-mono text-xs">meta.timings</code> (a partir da
          imagem <code className="font-mono text-xs">0.1.3</code>) é o que
          permite ao cliente separar fila de GPU sozinho:{" "}
          <code className="font-mono text-xs">worker_s</code> ={" "}
          <code className="font-mono text-xs">decode + gpu + encode</code>, e{" "}
          <code className="font-mono text-xs">queue_wait_s</code> fica de fora.
        </p>

        <div className="overflow-x-auto rounded-md border">
          <table className="w-full text-left text-xs">
            <thead className="border-b bg-muted/50 text-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">Ritmo</th>
                <th className="px-3 py-2 font-medium">Valor</th>
                <th className="px-3 py-2 font-medium">Observação</th>
              </tr>
            </thead>
            <tbody>
              <tr className="border-b">
                <td className="px-3 py-2">Submissões</td>
                <td className="px-3 py-2">10/min</td>
                <td className="px-3 py-2">
                  compartilhado pelas duas rotas
                </td>
              </tr>
              <tr className="border-b">
                <td className="px-3 py-2">Em voo</td>
                <td className="px-3 py-2">3</td>
                <td className="px-3 py-2">1 gerando + 2 na fila</td>
              </tr>
              <tr>
                <td className="px-3 py-2">Espera na fila</td>
                <td className="px-3 py-2">60s</td>
                <td className="px-3 py-2">
                  depois disso, <code className="font-mono text-xs">504</code>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p>
          Os limites são{" "}
          <span className="font-medium text-foreground">da stack</span>, não da
          chave — o pod é dedicado e gera uma imagem por vez. Não há cota de
          tokens (difusão não gera tokens). Tempos observados: ~6–9s para
          texto → imagem, ~7–21s para edição.
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
                  campo fora da faixa (
                  <code className="font-mono text-xs">invalid_size</code>,{" "}
                  <code className="font-mono text-xs">invalid_steps</code>…),
                  referência não-PNG/JPEG/WEBP, ou imagem mandada no JSON do{" "}
                  <code className="font-mono text-xs">generations</code>
                </td>
              </tr>
              <tr className="border-b">
                <td className="px-3 py-2 font-mono">404</td>
                <td className="px-3 py-2">
                  <code className="font-mono text-xs">model</code> diferente do
                  servido (só em{" "}
                  <code className="font-mono text-xs">edits</code>)
                </td>
              </tr>
              <tr className="border-b">
                <td className="px-3 py-2 font-mono">429</td>
                <td className="px-3 py-2">
                  fila cheia (
                  <code className="font-mono text-xs">queue_full</code>) ou
                  acima de 10/min — respeite o{" "}
                  <code className="font-mono text-xs">Retry-After</code>
                </td>
              </tr>
              <tr className="border-b">
                <td className="px-3 py-2 font-mono">502</td>
                <td className="px-3 py-2">
                  imagem gerada mas não armazenada — repetir é seguro, mas gera
                  de novo (seed nova, se não fixada)
                </td>
              </tr>
              <tr>
                <td className="px-3 py-2 font-mono">504</td>
                <td className="px-3 py-2">
                  <code className="font-mono text-xs">queue_timeout</code> —
                  esperou mais de 60s por uma vaga
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p>
          Dois formatos de erro convivem: o gateway devolve{" "}
          <code className="font-mono text-xs">{'{"detail": "..."}'}</code> e o
          pod devolve{" "}
          <code className="font-mono text-xs">
            {'{"error": {"code": ...}}'}
          </code>{" "}
          repassado byte a byte. Toda imagem gerada é armazenada por 30 dias,
          ligada à conta, stack e chave.
        </p>
      </div>

      <p className="text-xs text-muted-foreground">
        Erros comuns a todos os casos —{" "}
        <code className="font-mono">401</code> (chave ausente/inválida/
        revogada), <code className="font-mono">403</code> (limite de ambientes
        do plano), <code className="font-mono">404</code> (rota inexistente),{" "}
        <code className="font-mono">429</code>/<code className="font-mono">503</code>{" "}
        (rate limit, cota ou infraestrutura religando) — seguem as mesmas
        regras da aba &ldquo;Exemplos de chamada&rdquo; e de{" "}
        <code className="font-mono">docs/integracao.md</code>.
      </p>
    </div>
  )
}
