import { timingSafeEqual } from "crypto"
import { NextRequest, NextResponse } from "next/server"

import { flushGatewayKeyCache } from "@/lib/actions"
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

const INVALID = Symbol("invalid")

// number entre [lo, hi], ou null explícito (limpa o default) — undefined
// (campo ausente no body) não é aceito aqui, é filtrado antes de chamar isso.
function parseBoundedNumberOrNull(value: unknown, lo: number, hi: number): number | null | typeof INVALID {
  if (value === null) return null
  if (typeof value === "number" && Number.isFinite(value) && value >= lo && value <= hi) return value
  return INVALID
}

// Mesma forma de parseBoundedNumberOrNull, mas pra max_tokens: inteiro > min,
// sem teto no banco (a coluna só exige `> 0` — migration 0056; o teto real é
// o clamp de runtime do gateway, MAX_MAX_TOKENS). Espelha
// validateOptionalMaxTokens do TryStac (api-keys/actions.ts).
function parseIntegerOrNull(value: unknown, min: number): number | null | typeof INVALID {
  if (value === null) return null
  if (typeof value === "number" && Number.isInteger(value) && value > min) return value
  return INVALID
}

// Configura defaults de sampling (temperature/top_p/max_tokens/
// presence_penalty) aplicados pelo gateway quando o cliente final não manda o
// parâmetro na requisição (temperature/top_p: migration 0035; max_tokens/
// presence_penalty: migration 0056). Pensada para um sistema externo (ex.:
// LP/admin de outro projeto) configurar isso por stack sem precisar de sessão
// do painel.
export async function PATCH(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const unauthorized = checkSecret(req)
  if (unauthorized) return unauthorized

  const { id: stackId } = await params
  const body = await req.json().catch(() => null)
  if (!body || typeof body !== "object") {
    return NextResponse.json({ error: "corpo inválido" }, { status: 400 })
  }

  const update: Record<string, number | null> = {}
  if ("default_temperature" in body) {
    const v = parseBoundedNumberOrNull(body.default_temperature, 0, 2)
    if (v === INVALID) {
      return NextResponse.json(
        { error: "default_temperature deve ser number entre 0 e 2, ou null" },
        { status: 400 }
      )
    }
    update.default_temperature = v
  }
  if ("default_top_p" in body) {
    const v = parseBoundedNumberOrNull(body.default_top_p, 0, 1)
    if (v === INVALID) {
      return NextResponse.json(
        { error: "default_top_p deve ser number entre 0 e 1, ou null" },
        { status: 400 }
      )
    }
    update.default_top_p = v
  }
  if ("default_max_tokens" in body) {
    const v = parseIntegerOrNull(body.default_max_tokens, 0)
    if (v === INVALID) {
      return NextResponse.json(
        { error: "default_max_tokens deve ser um inteiro > 0, ou null" },
        { status: 400 }
      )
    }
    update.default_max_tokens = v
  }
  if ("default_presence_penalty" in body) {
    const v = parseBoundedNumberOrNull(body.default_presence_penalty, -2, 2)
    if (v === INVALID) {
      return NextResponse.json(
        { error: "default_presence_penalty deve ser number entre -2 e 2, ou null" },
        { status: 400 }
      )
    }
    update.default_presence_penalty = v
  }
  if (Object.keys(update).length === 0) {
    return NextResponse.json(
      {
        error:
          "informe default_temperature, default_top_p, default_max_tokens e/ou default_presence_penalty",
      },
      { status: 400 }
    )
  }

  const db = createSupabaseAdmin()
  const { error } = await db.from("stacks").update(update).eq("id", stackId)
  if (error) {
    return NextResponse.json({ error: error.message }, { status: 400 })
  }

  await flushGatewayKeyCache().catch((e) =>
    console.error("Flush do key cache falhou (a config foi salva, expira pelo TTL):", e)
  )

  return NextResponse.json({ ok: true, ...update })
}
