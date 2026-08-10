import { generateStackSlug, STACK_SLUG_RE } from "./slug"
import type { createSupabaseAdmin } from "./supabase/server"
import type { TemplatePlan } from "./types"

type Db = ReturnType<typeof createSupabaseAdmin>

export type InsertStackInput = {
  db: Db
  accountId: string
  plan: TemplatePlan
  /** Nome exibido ao cliente. Sem isto, cai no slug. */
  name?: string | null
  /** ISO date (YYYY-MM-DD). Omitido = default do banco (current_date). */
  purchaseDate?: string | null
  slug?: string | null
  /**
   * Chave de idempotência do provisionamento por checkout — o
   * `client_reference` de chargefy_checkout_attempts. Ver o unique parcial
   * `stacks_provisioning_ref_uidx` (migration 0050).
   */
  provisioningRef?: string | null
  /** Default do banco é 'active'; o checkout passa 'trialing' ou 'active'. */
  billingStatus?: string | null
}

export type InsertStackResult = {
  stackId: string
  slug: string
  /** false = já existia uma stack com este provisioningRef; nada foi criado. */
  created: boolean
}

/**
 * Insere a linha de `stacks` e nada mais — sem alocar máquina, sem emitir
 * chave. Vive fora de `lib/actions.ts` de propósito: aquele arquivo é
 * `"use server"`, e toda export dele vira uma Server Action com endpoint
 * próprio. Isto aqui é chamado por um route handler autenticado por segredo
 * (app/api/stacks/route.ts) e pela action de admin — nenhum dos dois quer
 * uma superfície pública extra.
 *
 * A separação "linha de stack" vs. "máquina + chave" é o que permite o
 * checkout responder em milissegundos: a alocação de GPU já é lazy em três
 * caminhos independentes (ensureStackMachine no painel, place_base_stack e
 * resolve_base_machine no gateway), então uma stack com machine_id NULL é um
 * estado normal e testado — é o mesmo em que o idle reaper deixa qualquer
 * stack ociosa.
 */
export async function insertStack(input: InsertStackInput): Promise<InsertStackResult> {
  const { db, accountId, plan, provisioningRef } = input

  // Caminho de idempotência: uma reentrega de webhook não pode virar uma
  // segunda stack (e, mais tarde, um segundo pod). Este SELECT resolve o caso
  // comum; a corrida real é resolvida pelo unique lá embaixo.
  if (provisioningRef) {
    const existing = await findStackByProvisioningRef(db, provisioningRef)
    if (existing) return { ...existing, created: false }
  }

  let slug = (input.slug ?? "").trim()
  if (!STACK_SLUG_RE.test(slug)) slug = generateStackSlug()

  for (let attempt = 0; attempt < 5; attempt++) {
    const { data, error } = await db
      .from("stacks")
      .insert({
        account_id: accountId,
        plan,
        slug,
        // `name` é coluna do TryStac (supabase/SHARED_SCHEMA.md), NOT NULL sem
        // default — sem isto o insert falha com 23502, que nem cai no retry
        // de colisão de slug abaixo.
        name: input.name?.trim() || slug,
        ...(input.purchaseDate ? { purchase_date: input.purchaseDate } : {}),
        ...(provisioningRef ? { provisioning_ref: provisioningRef } : {}),
        ...(input.billingStatus ? { billing_status: input.billingStatus } : {}),
      })
      .select("id")
      .single<{ id: string }>()

    if (!error && data) return { stackId: data.id, slug, created: true }
    if (!error) break

    if (error.code !== "23505") throw new Error(error.message)

    // Dois uniques podem estourar aqui e o tratamento é oposto, então não dá
    // pra assumir "23505 = colisão de slug" como antes desta coluna existir.
    const blob = `${error.message} ${error.details ?? ""} ${error.hint ?? ""}`
    if (provisioningRef && blob.includes("provisioning_ref")) {
      // Entrega concorrente do mesmo webhook ganhou a corrida. Ela criou a
      // stack que nós criaríamos — reler e devolver é o comportamento certo.
      const existing = await findStackByProvisioningRef(db, provisioningRef)
      if (existing) return { ...existing, created: false }
      throw new Error("provisioning_ref duplicado, mas a stack não foi encontrada")
    }

    slug = generateStackSlug()
  }

  throw new Error("Não foi possível gerar um ID único; tente novamente")
}

export async function findStackByProvisioningRef(
  db: Db,
  provisioningRef: string
): Promise<{ stackId: string; slug: string } | null> {
  const { data } = await db
    .from("stacks")
    .select("id, slug")
    .eq("provisioning_ref", provisioningRef)
    .maybeSingle<{ id: string; slug: string }>()
  return data ? { stackId: data.id, slug: data.slug } : null
}
