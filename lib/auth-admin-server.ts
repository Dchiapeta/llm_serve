// Checagem de acesso server-side.
//
// Separado de lib/auth-admin.ts porque este importa next/headers (via
// createSupabaseServerClient) e o proxy precisa da regra pura, sem isso.
//
// Esta é a camada que de fato garante o acesso. O proxy faz o corte otimista
// (evita a navegação), mas a doc do Next 16 é explícita em dizer que ele "should
// not be used as a full session management or authorization solution"
// (01-app/01-getting-started/16-proxy.md).

import { redirect } from "next/navigation"

import { bypassEnabled, isAllowedAdminClaims } from "./auth-admin"
import { createSupabaseServerClient } from "./supabase/server"

/**
 * Devolve o e-mail do usuário autenticado ou redireciona. Usar no layout do
 * dashboard e em qualquer route handler que sirva dado do painel.
 */
export async function requireAdminSession(): Promise<string> {
  if (bypassEnabled()) return "dev@local"

  const supabase = await createSupabaseServerClient()
  const { data } = await supabase.auth.getClaims()
  const claims = data?.claims ?? null
  const email = claims?.email as string | undefined

  if (!email) redirect("/login")
  if (!isAllowedAdminClaims(claims)) redirect("/sem-acesso")
  return email
}

/**
 * E-mail do admin da sessão corrente, ou null — SEM redirecionar. Pra autoria
 * de eventos (migration 0070): as ações de máquina também rodam em rotas
 * chamadas pelo gateway sem cookie (/api/machines/provision, /recreate), e o
 * redirect() de requireAdminSession lançaria NEXT_REDIRECT no meio de um
 * handler que o gateway só sabe ler como status + JSON. Não é controle de
 * acesso — quem barra continua sendo o layout do dashboard e o proxy.
 */
export async function currentAdminEmail(): Promise<string | null> {
  if (bypassEnabled()) return "dev@local"
  try {
    const supabase = await createSupabaseServerClient()
    const { data } = await supabase.auth.getClaims()
    const claims = data?.claims ?? null
    const email = claims?.email as string | undefined
    if (!email || !isAllowedAdminClaims(claims)) return null
    return email
  } catch {
    // fora de um request com cookies (ex.: chamada interna) não há sessão
    return null
  }
}
