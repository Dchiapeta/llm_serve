import { NextRequest, NextResponse } from "next/server"

import { agent } from "@/lib/agent"
import { createSupabaseAdmin } from "@/lib/supabase/server"
import type { Machine } from "@/lib/types"

export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params
  const db = createSupabaseAdmin()
  const { data: m } = await db
    .from("machines")
    .select("*")
    .eq("id", id)
    .single<Machine>()

  if (!m) {
    return NextResponse.json({ error: "Máquina não encontrada" }, { status: 404 })
  }

  const apiKeyId = req.nextUrl.searchParams.get("api_key_id") ?? undefined
  const tail = Number(req.nextUrl.searchParams.get("tail") ?? 200)

  try {
    const logs = await agent.logs(m, { apiKeyId, tail })
    return NextResponse.json(logs)
  } catch (e) {
    return NextResponse.json(
      {
        lines: [],
        error: `Agent inacessível: ${e instanceof Error ? e.message : String(e)}`,
      },
      { status: 200 }
    )
  }
}
