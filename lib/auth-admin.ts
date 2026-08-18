// Quem pode entrar no manager.
//
// Existe porque o painel compartilha o projeto Supabase — e portanto o mesmo
// Supabase Auth — com o app do cliente final (app.trystac.com). Até aqui o
// proxy.ts só verificava SE havia sessão, nunca de quem ela era: qualquer
// cliente logado no app conseguia abrir o manager e ver todas as máquinas,
// todos os clientes e as chaves de API em texto puro.
//
// A regra vive num arquivo só e é consumida pelo proxy (check otimista), pelo
// layout do dashboard (a camada que de fato garante), pela camada de dados do
// CRM e pelo signup. Duplicar a regra é como ela passa a divergir.

const DEFAULT_DOMAINS = ["trystac.com"]

/**
 * Bypass de desenvolvimento local. Replica exatamente a condição de proxy.ts —
 * sem isso, rodar com DEV_BYPASS_AUTH=1 (que não produz claims) quebraria em
 * todo layout que exige e-mail permitido.
 */
export function bypassEnabled(): boolean {
  return (
    process.env.DEV_BYPASS_AUTH === "1" &&
    process.env.NODE_ENV !== "production"
  )
}

/**
 * Domínios permitidos. O default está no código, não só na env var: se
 * ADMIN_EMAIL_DOMAINS não existir em produção, o comportamento correto tem que
 * ser o restritivo — uma env var faltando nunca pode abrir o painel.
 */
function allowedDomains(): string[] {
  const raw = process.env.ADMIN_EMAIL_DOMAINS
  if (!raw) return DEFAULT_DOMAINS
  const parsed = raw
    .split(",")
    .map((d) => d.trim().toLowerCase().replace(/^@/, ""))
    .filter(Boolean)
  return parsed.length > 0 ? parsed : DEFAULT_DOMAINS
}

/** Compara o trecho após o último "@", em minúsculas, por igualdade exata. */
export function isAllowedAdminEmail(email?: string | null): boolean {
  if (!email) return false
  const at = email.lastIndexOf("@")
  if (at === -1) return false
  const domain = email.slice(at + 1).trim().toLowerCase()
  return allowedDomains().includes(domain)
}

/** Subconjunto dos claims do JWT que o gate examina. */
export type AdminClaims = {
  email?: unknown
  email_verified?: unknown
  is_anonymous?: unknown
  user_metadata?: { email_verified?: unknown } | null
} | null

/**
 * O Supabase emite `email_verified` dentro de `user_metadata`, não no topo do
 * JWT. Ler só a raiz deixava a guarda morta — ela nunca reprovava ninguém.
 * Os dois lugares são consultados porque a posição já variou entre versões do
 * GoTrue, e ler só um seria apostar na versão do servidor.
 */
function emailVerified(claims: NonNullable<AdminClaims>): boolean | null {
  const root = claims.email_verified
  if (typeof root === "boolean") return root
  const nested = claims.user_metadata?.email_verified
  if (typeof nested === "boolean") return nested
  return null
}

/**
 * Decide o acesso a partir dos claims inteiros, não só do e-mail.
 *
 * ATENÇÃO — o domínio NÃO é prova de identidade enquanto o projeto Supabase
 * estiver com `mailer_autoconfirm` ligado e signup aberto (era o caso em
 * 17/08/2026): qualquer pessoa se cadastra como algo@trystac.com pelo app do
 * cliente e é confirmada sozinha, sem nunca acessar a caixa de entrada. As
 * checagens abaixo só passam a valer de fato depois que o autoconfirm for
 * desligado — até lá são defesa em profundidade, não garantia.
 *
 * `email_verified` ausente não bloqueia: nem todo projeto o emite no JWT, e
 * negar por omissão trancaria admins legítimos para fora.
 */
export function isAllowedAdminClaims(claims: AdminClaims): boolean {
  if (!claims) return false
  if (claims.is_anonymous === true) return false
  if (emailVerified(claims) === false) return false
  return isAllowedAdminEmail(
    typeof claims.email === "string" ? claims.email : null
  )
}

/** Rótulo dos domínios permitidos, para mensagens ao usuário. */
export function allowedDomainsLabel(): string {
  return allowedDomains()
    .map((d) => `@${d}`)
    .join(", ")
}
