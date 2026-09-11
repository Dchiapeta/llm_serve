import { timingSafeEqual } from "crypto"
import { NextRequest, NextResponse } from "next/server"

import { createKey, flushGatewayKeyCache } from "@/lib/actions"
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
// "customer" de verdade — hash, prefixo, cota por stack e sync ao gateway,
// tudo já resolvido por createKey. Nunca a chave interna de Playground (essa
// segue por /api/playground/key).
//
// NÃO aloca máquina: a chave nasce com o machine_id que a stack tiver (null é
// normal) e o gateway homeia a stack na PRIMEIRA request (resolve_base_machine
// → place_base_stack → rebind_stack_keys, docker/gateway/main.py). Antes esta
// rota chamava ensureStackMachine e podia criar pod no RunPod de forma
// síncrona: estourava o teto de 15s do panelFetch do painel do cliente e
// qualquer falta de GPU virava erro na criação da chave.
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

  // Escolha do usuário no painel do cliente (repo separado) sobre consultar a
  // base de conhecimento nesta chave (migration 0065). Ausente = createKey
  // grava null, o comportamento legado — mas rejeitamos tipo errado em vez de
  // cair no legado em silêncio: "enable_knowledge_base": "true" (string) viraria
  // uma chave que ignora a escolha do usuário sem ninguém perceber.
  const enableKnowledgeBase: unknown = body?.enable_knowledge_base
  if (enableKnowledgeBase !== undefined && typeof enableKnowledgeBase !== "boolean") {
    return NextResponse.json(
      { error: "enable_knowledge_base deve ser boolean" },
      { status: 400 }
    )
  }

  const db = createSupabaseAdmin()
  const { data: stack } = await db
    .from("stacks")
    .select("id, account_id, machine_id")
    .eq("id", stackId)
    .single<{ id: string; account_id: string; machine_id: string | null }>()

  if (!stack) {
    return NextResponse.json({ error: "Stack não encontrada" }, { status: 400 })
  }

  try {
    // stack.machine_id null (idle reaper liberou a vaga, ou a stack nunca foi
    // homeada) é aceito: mesmo caminho da chave de Playground
    // (getOrCreatePlaygroundKey). Quem chama esta rota não espera pod nenhum.
    const { plainKey } = await createKey({
      accountId: stack.account_id,
      machineId: stack.machine_id,
      stackId,
      name,
      expiresAt,
      purpose: "customer",
      enableKnowledgeBase,
    })
    return NextResponse.json({ plain_key: plainKey })
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : String(e) },
      { status: 400 }
    )
  }
}

// Mesma integração externa confiável do POST (checkSecret), porque quem
// configura a chave é o painel do cliente. null volta a herdar a stack
// (default_enable_thinking) ou o comportamento legado de RAG
// (enable_knowledge_base).
//
// Campo a campo com `in body`, no estilo de app/api/stacks/[id]/model-config:
// antes o guard EXIGIA default_enable_thinking, então um payload que só mexesse
// na base de conhecimento tomava 400. Só o api_key_id é obrigatório; o resto é
// "pelo menos um".
export async function PATCH(req: NextRequest) {
  const unauthorized = checkSecret(req)
  if (unauthorized) return unauthorized
  const body = await req.json().catch(() => null)
  if (!body || typeof body !== "object") {
    return NextResponse.json({ error: "corpo inválido" }, { status: 400 })
  }
  if (typeof body.api_key_id !== "string" || !body.api_key_id) {
    return NextResponse.json({ error: "api_key_id (string) é obrigatório" }, { status: 400 })
  }

  const update: Record<string, boolean | null> = {}
  if ("default_enable_thinking" in body) {
    const value = body.default_enable_thinking
    if (value !== null && typeof value !== "boolean") {
      return NextResponse.json({ error: "default_enable_thinking deve ser boolean ou null" }, { status: 400 })
    }
    update.default_enable_thinking = value
  }
  if ("enable_knowledge_base" in body) {
    // null é aceito aqui (volta a chave ao comportamento legado) mesmo que
    // nenhuma interface o envie — é a mesma forma dos outros campos, e a coluna
    // aceita os três estados (migration 0065).
    const value = body.enable_knowledge_base
    if (value !== null && typeof value !== "boolean") {
      return NextResponse.json({ error: "enable_knowledge_base deve ser boolean ou null" }, { status: 400 })
    }
    update.enable_knowledge_base = value
  }
  if (Object.keys(update).length === 0) {
    return NextResponse.json(
      { error: "informe ao menos um de: default_enable_thinking, enable_knowledge_base" },
      { status: 400 }
    )
  }

  const db = createSupabaseAdmin()
  const { data, error } = await db.from("api_keys")
    .update(update)
    .eq("id", body.api_key_id).select("id").maybeSingle()
  if (error) return NextResponse.json({ error: error.message }, { status: 400 })
  if (!data) return NextResponse.json({ error: "Chave não encontrada" }, { status: 404 })
  await flushGatewayKeyCache().catch((error) =>
    console.error("Configuração salva; cache será renovado pelo TTL:", error)
  )
  return NextResponse.json({ ok: true, ...update })
}
