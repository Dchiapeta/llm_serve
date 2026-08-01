import { timingSafeEqual } from "crypto"
import { NextRequest, NextResponse } from "next/server"

import { createKey } from "@/lib/actions"
import { createSupabaseAdmin } from "@/lib/supabase/server"

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

// Chamada pelo painel admin do cliente (repo separado) pra emitir uma chave
// "customer" de verdade — com checagem de capacidade da máquina, hash,
// prefixo e sync ao gateway, tudo já resolvido por createKey. Nunca a chave
// interna de Playground (essa segue por /api/playground/key).
export async function POST(req: NextRequest) {
  const unauthorized = checkSecret(req)
  if (unauthorized) return unauthorized

  const body = await req.json().catch(() => null)
  const stackId = body?.stack_id
  if (typeof stackId !== "string" || !stackId) {
    return NextResponse.json({ error: "stack_id (string) é obrigatório" }, { status: 400 })
  }
  const name = typeof body?.name === "string" ? body.name : null
  const expiresAt = typeof body?.expires_at === "string" ? body.expires_at : null

  const db = createSupabaseAdmin()
  const { data: stack } = await db
    .from("stacks")
    .select("id, account_id, machine_id")
    .eq("id", stackId)
    .single<{ id: string; account_id: string; machine_id: string | null }>()

  if (!stack) {
    return NextResponse.json({ error: "Stack não encontrada" }, { status: 400 })
  }
  if (!stack.machine_id) {
    return NextResponse.json(
      { error: "A máquina desta stack ainda não está pronta para emitir chaves." },
      { status: 400 }
    )
  }

  try {
    const { plainKey } = await createKey({
      accountId: stack.account_id,
      machineId: stack.machine_id,
      stackId,
      name,
      expiresAt,
      purpose: "customer",
    })
    return NextResponse.json({ plain_key: plainKey })
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : String(e) },
      { status: 400 }
    )
  }
}
