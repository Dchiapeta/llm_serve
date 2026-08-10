import { timingSafeEqual } from "crypto"
import { NextRequest, NextResponse } from "next/server"

import { findStackByProvisioningRef, insertStack } from "@/lib/stacks"
import { createSupabaseAdmin } from "@/lib/supabase/server"
import { TEMPLATE_PLANS, type TemplatePlan } from "@/lib/types"

function secretsMatch(a: string, b: string): boolean {
  const bufA = Buffer.from(a)
  const bufB = Buffer.from(b)
  return bufA.length === bufB.length && timingSafeEqual(bufA, bufB)
}

function checkSecret(req: NextRequest): NextResponse | null {
  const secret = req.headers.get("x-external-secret")
  if (
    !process.env.EXTERNAL_INTEGRATION_SECRET ||
    !secret ||
    !secretsMatch(secret, process.env.EXTERNAL_INTEGRATION_SECRET)
  ) {
    return NextResponse.json({ error: "secret inválido" }, { status: 401 })
  }
  return null
}

// O checkout usa os ids em minúsculo da Chargefy; `stacks.plan` tem CHECK
// capitalizado (stacks_plan_valid). Mapa explícito em vez de capitalizar a
// string: errar aqui só apareceria como 23514 em produção, depois do cliente
// já ter pago.
const PLAN_BY_SLUG: Record<string, TemplatePlan> = {
  go: "Go",
  pro: "Pro",
  max: "Max",
  enterprise: "Enterprise",
}

function parsePlan(raw: unknown): TemplatePlan | null {
  if (typeof raw !== "string") return null
  const normalized = PLAN_BY_SLUG[raw.trim().toLowerCase()]
  if (normalized) return normalized
  return TEMPLATE_PLANS.includes(raw as TemplatePlan) ? (raw as TemplatePlan) : null
}

// Provisiona a stack de um pagamento confirmado. Chamada pelo handler de
// webhook da Chargefy (repo TryStac), nunca pelo browser.
//
// NÃO aloca máquina nem emite chave: cria só a linha de `stacks` e volta em
// milissegundos. Subir pod aqui significaria manter uma função serverless
// aberta por ~1min dentro de um webhook — a Chargefy desistiria e reentregaria,
// e a reentrega criaria um segundo pod. A alocação é lazy e já existe em três
// caminhos (ensureStackMachine, place_base_stack, resolve_base_machine).
//
// Idempotente por `provisioning_ref`: a entrega é at-least-once (até 9
// tentativas em ~3 dias), então reprocessar o mesmo pagamento tem que devolver
// a MESMA stack, não criar outra.
export async function POST(req: NextRequest) {
  const unauthorized = checkSecret(req)
  if (unauthorized) return unauthorized

  const body = await req.json().catch(() => null)

  const plan = parsePlan(body?.plan)
  if (!plan) {
    return NextResponse.json(
      { error: "plan inválido (esperado: go, pro, max ou enterprise)" },
      { status: 400 }
    )
  }

  const db = createSupabaseAdmin()

  // Troca de plano de uma stack que já existe. Sem este ramo, um upgrade
  // chegaria aqui com um client_reference novo, não casaria com
  // provisioning_ref nenhum, e criaria uma SEGUNDA stack para o mesmo cliente.
  const stackId = typeof body?.stack_id === "string" ? body.stack_id : null
  if (stackId) {
    // Sem produto para o plano novo, a stack fica sem nenhuma máquina elegível
    // e todo request dela vira 503 permanente. Falhar aqui devolve um erro de
    // configuração legível em vez de um pod que nunca sobe.
    const { data: targetTpl } = await db
      .from("templates")
      .select("id")
      .eq("plan", plan)
      .limit(1)
      .maybeSingle<{ id: string }>()
    if (!targetTpl) {
      return NextResponse.json(
        { error: `Nenhum produto configurado para o plano ${plan}` },
        { status: 400 }
      )
    }

    // `machine_id: null` junto com o plano é o ponto central deste ramo. A
    // máquina atual roda o template do plano ANTIGO, e o gateway
    // (resolve_base_machine) serve qualquer máquina running já apontada pela
    // stack sem revalidar template — um upgrade Go→Pro passaria a cobrar Pro,
    // liberaria rate limit e contexto de Pro, e continuaria servindo o modelo
    // do Go. Zerar a vaga força place_base_stack a re-homear na próxima
    // request, no template certo. É o mesmo movimento (contábil, sem tocar no
    // pod) que o idle reaper já faz.
    const { data: updated, error } = await db
      .from("stacks")
      .update({ plan, machine_id: null })
      .eq("id", stackId)
      .select("id, slug")
      .maybeSingle<{ id: string; slug: string }>()
    if (error) return NextResponse.json({ error: error.message }, { status: 400 })
    if (!updated) {
      return NextResponse.json({ error: "Stack não encontrada" }, { status: 400 })
    }
    return NextResponse.json({ stack_id: updated.id, slug: updated.slug, created: false })
  }

  const provisioningRef = typeof body?.provisioning_ref === "string" ? body.provisioning_ref : null
  if (!provisioningRef) {
    return NextResponse.json(
      { error: "provisioning_ref (uuid) é obrigatório" },
      { status: 400 }
    )
  }

  // Já provisionado numa entrega anterior — resposta idêntica, sem efeito.
  const already = await findStackByProvisioningRef(db, provisioningRef)
  if (already) {
    return NextResponse.json({ stack_id: already.stackId, slug: already.slug, created: false })
  }

  const accountId = typeof body?.account_id === "string" ? body.account_id : null
  if (!accountId) {
    return NextResponse.json({ error: "account_id (uuid) é obrigatório" }, { status: 400 })
  }

  // Só aceita conta que já existe. Resolver por e-mail (como o fluxo do painel
  // admin faz) seria perigoso aqui: `accounts` não tem unique em email, então
  // um e-mail não encontrado criaria uma conta nova com user_id NULL — o
  // cliente pagaria e não veria stack nenhuma no painel, porque o TryStac
  // lista por accounts.user_id.
  const { data: account } = await db
    .from("accounts")
    .select("id")
    .eq("id", accountId)
    .maybeSingle<{ id: string }>()
  if (!account) {
    return NextResponse.json({ error: "Conta não encontrada" }, { status: 400 })
  }

  // Vender um plano sem produto cadastrado geraria uma stack que nunca
  // consegue alocar máquina. Falha aqui é um erro de configuração nosso e
  // precisa aparecer como tal, não como um pod que não sobe.
  const { data: tpl } = await db
    .from("templates")
    .select("id")
    .eq("plan", plan)
    .limit(1)
    .maybeSingle<{ id: string }>()
  if (!tpl) {
    return NextResponse.json(
      { error: `Nenhum produto configurado para o plano ${plan}` },
      { status: 400 }
    )
  }

  const billingStatus = body?.billing_status === "trialing" ? "trialing" : "active"
  const name = typeof body?.name === "string" ? body.name : null

  try {
    const result = await insertStack({
      db,
      accountId,
      plan,
      name,
      provisioningRef,
      billingStatus,
    })
    return NextResponse.json({
      stack_id: result.stackId,
      slug: result.slug,
      created: result.created,
    })
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : String(e) },
      { status: 400 }
    )
  }
}
