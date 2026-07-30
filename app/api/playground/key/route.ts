import { timingSafeEqual } from "crypto"
import { NextRequest, NextResponse } from "next/server"

import { getOrCreatePlaygroundKey } from "@/lib/actions"

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

// Chamada pelo painel admin do cliente (repo separado) pra obter a chave
// interna de Playground (texto puro) de uma stack — nunca a chave "customer"
// do cliente. Cria a chave sob demanda se a stack ainda não tiver uma
// (stacks criadas antes desta feature existir).
export async function POST(req: NextRequest) {
  const unauthorized = checkSecret(req)
  if (unauthorized) return unauthorized

  const body = await req.json().catch(() => null)
  const stackId = body?.stack_id
  if (typeof stackId !== "string" || !stackId) {
    return NextResponse.json({ error: "stack_id (string) é obrigatório" }, { status: 400 })
  }

  try {
    const { plainKey } = await getOrCreatePlaygroundKey(stackId)
    return NextResponse.json({ plain_key: plainKey })
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : String(e) },
      { status: 400 }
    )
  }
}
