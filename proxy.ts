import { createServerClient } from "@supabase/ssr"
import { NextResponse, type NextRequest } from "next/server"

import { isAllowedAdminClaims } from "@/lib/auth-admin"

export async function proxy(request: NextRequest) {
  let response = NextResponse.next({ request })

  // apenas para desenvolvimento local sem Supabase configurado — o check
  // de NODE_ENV é defesa em profundidade contra a env var vazar pra um
  // deploy de produção por engano (ex.: copiada de um .env de dev)
  if (process.env.DEV_BYPASS_AUTH === "1" && process.env.NODE_ENV !== "production") {
    return response
  }

  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    {
      cookies: {
        getAll() {
          return request.cookies.getAll()
        },
        setAll(cookiesToSet) {
          cookiesToSet.forEach(({ name, value }) =>
            request.cookies.set(name, value)
          )
          response = NextResponse.next({ request })
          cookiesToSet.forEach(({ name, value, options }) =>
            response.cookies.set(name, value, options)
          )
        },
      },
    }
  )

  // getClaims valida o JWT localmente (via JWKS) quando o projeto usa signing
  // keys assimétricas — sem round-trip ao Supabase Auth a cada navegação. Com
  // HS256 faz fallback seguro para getUser(); nunca confia num JWT não validado.
  const {
    data: claims,
  } = await supabase.auth.getClaims()
  const user = claims?.claims ?? null

  const { pathname } = request.nextUrl
  const isDenied = pathname.startsWith("/sem-acesso")
  const isAuthPage =
    pathname.startsWith("/login") || pathname.startsWith("/signup")
  // /sem-acesso é pública no sentido do gate: é justamente onde o barrado cai,
  // e mandá-lo de volta pra ela criaria um laço de redirect.
  const isPublic = isAuthPage || isDenied

  if (!user && !isPublic) {
    const url = request.nextUrl.clone()
    url.pathname = "/login"
    return NextResponse.redirect(url)
  }

  // Ter sessão não basta: este painel divide o Supabase Auth com o app do
  // cliente final, então um cliente logado lá chega aqui autenticado. Só
  // e-mail de domínio permitido entra. Este é o corte otimista (evita a
  // navegação); quem de fato garante é o layout do dashboard — a doc do Next
  // 16 avisa que proxy "should not be used as a full ... authorization
  // solution" (01-app/01-getting-started/16-proxy.md).
  const allowed = isAllowedAdminClaims(user)

  if (user && !allowed && !isDenied) {
    const url = request.nextUrl.clone()
    url.pathname = "/sem-acesso"
    url.search = ""
    return NextResponse.redirect(url)
  }

  if (user && allowed && isPublic) {
    const url = request.nextUrl.clone()
    url.pathname = "/"
    return NextResponse.redirect(url)
  }

  return response
}

export const config = {
  matcher: [
    // protege tudo exceto assets estáticos e rotas chamadas por serviços
    // externos sem sessão de usuário (autenticadas pelo próprio secret no
    // handler): a rota de sync do agent, a de provisionamento automático e a
    // de recriação de máquina — chamadas pelo gateway com
    // X-Admin-Secret/PANEL_ADMIN_SECRET; e a de ingestão de RAG, a de config
    // de modelo por stack, a de emissão de chave "customer" e a de obtenção
    // da chave de Playground — chamadas por sistemas externos (LP/admin de
    // outro projeto) com X-External-Secret/EXTERNAL_INTEGRATION_SECRET. As
    // rotas com id na URL usam segmento dinâmico ([^/]+).
    //
    // `api/stacks$` é ancorado de propósito: sem o `$`, o lookahead casaria
    // qualquer coisa sob /api/stacks/... e tiraria do middleware rotas de
    // painel futuras que dependem de sessão. As duas rotas de cobrança
    // (criação de stack pelo checkout e billing-sync) conferem o mesmo
    // X-External-Secret no handler — ficar fora daqui não abre nada, e ficar
    // DENTRO as fazia responder 307 /login ao webhook da Chargefy: o cliente
    // pagava e a stack nunca nascia.
    "/((?!_next/static|_next/image|favicon.ico|api/agent|api/machines/provision|api/machines/[^/]+/recreate|api/knowledge/ingest|api/stacks$|api/stacks/[^/]+/billing-sync|api/stacks/[^/]+/model-config|api/playground/key|api/keys).*)",
  ],
}
