import { timingSafeEqual } from "crypto"
import { NextRequest, NextResponse } from "next/server"

import { deleteKnowledgeFile, ingestKnowledgeFile } from "@/lib/actions"

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

// Chamada por um sistema externo (ex.: LP/admin de outro projeto) DEPOIS de
// ele já ter subido o arquivo cru direto no bucket "knowledge" do Storage
// (com a service role key deste mesmo projeto Supabase), no path
// "{stack_id}/{filename}". Esta rota só dispara o processamento
// (chunk+embedding+index) — não recebe o arquivo em si.
export async function POST(req: NextRequest) {
  const unauthorized = checkSecret(req)
  if (unauthorized) return unauthorized

  const body = await req.json().catch(() => null)
  const accountId = body?.account_id
  const stackId = body?.stack_id
  const storagePath = body?.storage_path
  if (
    typeof accountId !== "string" || !accountId ||
    typeof stackId !== "string" || !stackId ||
    typeof storagePath !== "string" || !storagePath
  ) {
    return NextResponse.json(
      { error: "account_id, stack_id e storage_path (string) são obrigatórios" },
      { status: 400 }
    )
  }

  try {
    const result = await ingestKnowledgeFile({ accountId, stackId, storagePath })
    return NextResponse.json(result)
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : String(e) },
      { status: 400 }
    )
  }
}

// Remove um arquivo já indexado (Storage + chunks) — espelha deleteKnowledgeFile.
export async function DELETE(req: NextRequest) {
  const unauthorized = checkSecret(req)
  if (unauthorized) return unauthorized

  const body = await req.json().catch(() => null)
  const stackId = body?.stack_id
  const storagePath = body?.storage_path
  if (typeof stackId !== "string" || !stackId || typeof storagePath !== "string" || !storagePath) {
    return NextResponse.json(
      { error: "stack_id e storage_path (string) são obrigatórios" },
      { status: 400 }
    )
  }

  try {
    await deleteKnowledgeFile(stackId, storagePath)
    return NextResponse.json({ ok: true })
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : String(e) },
      { status: 400 }
    )
  }
}
