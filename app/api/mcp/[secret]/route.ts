import { timingSafeEqual } from "crypto"

import { WebStandardStreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js"

import { buildMcpServer } from "./server"

export const dynamic = "force-dynamic"

/**
 * Conector MCP do Claude (claude.ai → Settings → Connectors → custom connector),
 * de uso exclusivo da equipe — por isso autentica pelo segredo no caminho da
 * URL, e não por OAuth: o conector do claude.ai não manda header fixo.
 *
 * Quem tem a URL lê o CRM inteiro (clientes, receita, custo). Ela não pode ir
 * para chat, issue ou doc; para revogar, troque MCP_SECRET e faça redeploy.
 *
 * Fora do proxy.ts (sem cookie de sessão, ele mandaria para /login). Segredo
 * errado ou ausente responde 404, não 401: a rota não admite que existe.
 */

// 32 bytes em hex = 64 caracteres. Abaixo disso a env var foi preenchida à mão
// com algo adivinhável — melhor a rota não existir do que existir fraca.
const MIN_SECRET_LENGTH = 32

function authorized(secret: string): boolean {
  const expected = process.env.MCP_SECRET
  if (!expected || expected.length < MIN_SECRET_LENGTH) return false
  const a = Buffer.from(secret)
  const b = Buffer.from(expected)
  return a.length === b.length && timingSafeEqual(a, b)
}

/**
 * Origem que o cliente usou, para as URLs absolutas dos ícones. `req.url` não
 * serve: o Next o reescreve (em dev vira localhost mesmo pedindo 127.0.0.1) e,
 * atrás de proxy, pode ser o host interno.
 */
function publicOrigin(req: Request): string {
  const url = new URL(req.url)
  const host = req.headers.get("x-forwarded-host") ?? req.headers.get("host") ?? url.host
  const proto = req.headers.get("x-forwarded-proto") ?? url.protocol.replace(":", "")
  return `${proto}://${host}`
}

async function handle(
  req: Request,
  { params }: { params: Promise<{ secret: string }> }
): Promise<Response> {
  const { secret } = await params
  if (!authorized(secret)) return new Response("Not Found", { status: 404 })

  // Stateless: um servidor e um transporte por request. Não há estado de sessão
  // a guardar (as ferramentas só leem), e serverless não garante que a próxima
  // request caia na mesma instância.
  const server = buildMcpServer(publicOrigin(req))
  const transport = new WebStandardStreamableHTTPServerTransport({
    sessionIdGenerator: undefined,
    enableJsonResponse: true,
  })
  await server.connect(transport)
  return transport.handleRequest(req)
}

export { handle as GET, handle as POST, handle as DELETE }
