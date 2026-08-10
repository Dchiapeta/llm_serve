import { timingSafeEqual } from "crypto"
import { NextRequest, NextResponse } from "next/server"

import { flushGatewayKeyCache } from "@/lib/actions"

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

// Propaga na hora uma mudança de estado de cobrança para o gateway.
//
// `stacks.billing_status` é escrita por um trigger do banco (projeção de
// chargefy_subscriptions), então nenhuma aplicação participa da escrita e nada
// invalida o key_cache do gateway — a mudança levaria até KEY_CACHE_TTL_S (60s)
// para valer. Esperar 60s para SUSPENDER é irrelevante; esperar 60s para
// RELIBERAR quem acabou de pagar não é: o cliente volta do checkout, testa a
// API e continua tomando 402 sem entender por quê.
//
// Corpo vazio, e o `id` da stack é só para rastreio: o gateway não expõe flush
// por stack, e limpar o cache inteiro é barato (as entradas se repovoam na
// próxima request de cada chave).
export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const unauthorized = checkSecret(req)
  if (unauthorized) return unauthorized

  const { id: stackId } = await params

  try {
    await flushGatewayKeyCache()
  } catch (e) {
    // Best-effort por contrato: o estado já está correto no banco e o TTL
    // resolve sozinho. Quem chama (o webhook da Chargefy) não pode falhar por
    // causa disto — falhar ali faria a Chargefy reentregar o evento inteiro.
    console.error(`Flush do key cache falhou para a stack ${stackId}:`, e)
    return NextResponse.json({ ok: false, flushed: false })
  }

  return NextResponse.json({ ok: true, flushed: true })
}
