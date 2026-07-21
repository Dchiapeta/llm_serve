import { timingSafeEqual } from "crypto"
import { NextRequest, NextResponse } from "next/server"

import { recreateMachine } from "@/lib/actions"

function secretsMatch(a: string, b: string): boolean {
  const bufA = Buffer.from(a)
  const bufB = Buffer.from(b)
  return bufA.length === bufB.length && timingSafeEqual(bufA, bufB)
}

// Chamada pelo gateway (docker/gateway/main.py) quando o auto-wake de um pod
// pausado falha por "not enough free GPUs" — o host cedeu a GPU e religar é
// impossível até recriar o pod num host novo. Mesmo secret dedicado do
// /provision (PANEL_ADMIN_SECRET): recriar tem custo real de GPU. A decisão de
// QUANDO chamar é toda do gateway; esta rota só executa a recriação pedida.
export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const secret = req.headers.get("x-admin-secret")
  if (
    !process.env.PANEL_ADMIN_SECRET ||
    !secret ||
    !secretsMatch(secret, process.env.PANEL_ADMIN_SECRET)
  ) {
    return NextResponse.json({ error: "admin secret inválido" }, { status: 401 })
  }

  const { id } = await params
  let result: Awaited<ReturnType<typeof recreateMachine>>
  try {
    result = await recreateMachine(id)
  } catch (e) {
    // recreateMachine (RunPod/Supabase) pode lançar fora dos caminhos de erro
    // já tratados — nunca deixa o gateway receber o 500 HTML padrão do Next
    // (ele só interpreta status_code + corpo JSON).
    return NextResponse.json(
      { error: e instanceof Error ? e.message : String(e) },
      { status: 502 }
    )
  }
  if (result && "error" in result) {
    return NextResponse.json({ error: result.error }, { status: 502 })
  }

  return NextResponse.json({ machine_id: id, status: "creating" })
}
