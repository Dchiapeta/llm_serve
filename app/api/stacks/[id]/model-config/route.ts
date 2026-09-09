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

// Inteiro dentro de [lo, hi], ou null explícito. Separado de parseIntegerOrNull
// porque aquele existe justamente para o caso SEM teto (max_tokens); steps tem
// um teto real e baixo, vindo do pod.
function parseBoundedIntegerOrNull(
  value: unknown,
  lo: number,
  hi: number
): number | null | typeof INVALID {
  if (value === null) return null
  if (typeof value === "number" && Number.isInteger(value) && value >= lo && value <= hi) return value
  return INVALID
}

// Tamanhos aceitos pelo pod de difusão (IMAGE_ALLOWED_SIZES em
// docker/image/server.py) e replicados no CHECK da migration 0062. Lista
// fechada dos dois lados: o pod recusa o resto com 400.
const IMAGE_SIZES = ["1024x1024", "1536x1024", "1024x1536"]

// Como parseBoundedNumberOrNull, mas para o `size`: um da lista, ou null
// explícito (limpa o default).
function parseImageSizeOrNull(value: unknown): string | null | typeof INVALID {
  if (value === null) return null
  if (typeof value === "string" && IMAGE_SIZES.includes(value)) return value
  return INVALID
}

// Configura defaults de sampling (temperature/top_p/max_tokens/
// presence_penalty) aplicados pelo gateway quando o cliente final não manda o
// parâmetro na requisição (temperature/top_p: migration 0035; max_tokens/
// presence_penalty: migration 0056). Pensada para um sistema externo (ex.:
// LP/admin de outro projeto) configurar isso por stack sem precisar de sessão
// do painel.
//
// Desde a migration 0062 a mesma rota configura os defaults de GERAÇÃO DE
// IMAGEM (default_image_size/default_image_steps/
// default_image_guidance_scale), aplicados pelo gateway em
// apply_stack_image_defaults. Uma rota só, e não duas, porque os dois conjuntos
// respondem à mesma pergunta ("o que a stack usa quando o cliente não diz") e
// vêm da mesma tela do TryStac — a de Comportamento, que só troca quais campos
// mostra conforme stacks.category. Os conjuntos são disjuntos na prática:
// nenhuma stack usa os dois, porque nenhuma serve texto e imagem ao mesmo
// tempo. O `in body` de cada campo é o que mantém isso verdadeiro — o payload
// de uma stack de imagem não menciona os campos de sampling, e vice-versa.
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

  const update: Record<string, number | string | null> = {}
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
  if ("default_image_size" in body) {
    const v = parseImageSizeOrNull(body.default_image_size)
    if (v === INVALID) {
      return NextResponse.json(
        { error: `default_image_size deve ser um de ${IMAGE_SIZES.join(", ")}, ou null` },
        { status: 400 }
      )
    }
    update.default_image_size = v
  }
  if ("default_image_steps" in body) {
    // Teto 8, e não "sem teto" como o de max_tokens: o checkpoint é destilado
    // e produz imagem pronta em pouquíssimos passos (STEPS_MAX no pod). Aceitar
    // 50 aqui seria gravar um default que só queima GPU.
    const v = parseBoundedIntegerOrNull(body.default_image_steps, 1, 8)
    if (v === INVALID) {
      return NextResponse.json(
        { error: "default_image_steps deve ser um inteiro entre 1 e 8, ou null" },
        { status: 400 }
      )
    }
    update.default_image_steps = v
  }
  if ("default_image_guidance_scale" in body) {
    const v = parseBoundedNumberOrNull(body.default_image_guidance_scale, 0, 20)
    if (v === INVALID) {
      return NextResponse.json(
        { error: "default_image_guidance_scale deve ser number entre 0 e 20, ou null" },
        { status: 400 }
      )
    }
    update.default_image_guidance_scale = v
  }
  if (Object.keys(update).length === 0) {
    return NextResponse.json(
      {
        error:
          "informe ao menos um de: default_temperature, default_top_p, " +
          "default_max_tokens, default_presence_penalty, default_image_size, " +
          "default_image_steps, default_image_guidance_scale",
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
