import { createServerClient } from "@supabase/ssr"
import { NextResponse, type NextRequest } from "next/server"

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
  const isAuthPage =
    pathname.startsWith("/login") || pathname.startsWith("/signup")

  if (!user && !isAuthPage) {
    const url = request.nextUrl.clone()
    url.pathname = "/login"
    return NextResponse.redirect(url)
  }

  if (user && isAuthPage) {
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
    // de recriação de máquina — as duas últimas chamadas pelo gateway com
    // X-Admin-Secret/PANEL_ADMIN_SECRET. O /recreate tem o id da máquina como
    // segmento dinâmico ([^/]+), ao contrário do /provision (path fixo).
    "/((?!_next/static|_next/image|favicon.ico|api/agent|api/machines/provision|api/machines/[^/]+/recreate).*)",
  ],
}
