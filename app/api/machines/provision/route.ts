import { NextRequest, NextResponse } from "next/server"

import { provisionMachineForPlan } from "@/lib/actions"
import { TEMPLATE_PLANS, type TemplatePlan } from "@/lib/types"

// Chamada pelo gateway (docker/gateway/main.py) quando decide, via watermark
// de slots livres do plano, que vale a pena criar uma máquina nova. Guardada
// por um secret dedicado (PANEL_ADMIN_SECRET) — não reaproveita
// GATEWAY_ADMIN_SECRET, que hoje só protege ações sem custo de GPU; criar
// pod é uma ação com custo real, então merece um secret de blast radius
// isolado. A decisão de QUANDO chamar é toda do gateway; esta rota só
// executa o provisionamento pedido.
export async function POST(req: NextRequest) {
  const secret = req.headers.get("x-admin-secret")
  if (!process.env.PANEL_ADMIN_SECRET || secret !== process.env.PANEL_ADMIN_SECRET) {
    return NextResponse.json({ error: "admin secret inválido" }, { status: 401 })
  }

  const body = await req.json().catch(() => null)
  const plan = body?.plan
  if (typeof plan !== "string" || !TEMPLATE_PLANS.includes(plan as TemplatePlan)) {
    return NextResponse.json({ error: "plan inválido" }, { status: 400 })
  }
  const templateId = typeof body?.template_id === "string" ? body.template_id : null

  let result: Awaited<ReturnType<typeof provisionMachineForPlan>>
  try {
    result = await provisionMachineForPlan({ plan: plan as TemplatePlan, templateId })
  } catch (e) {
    // provisionMachine (RunPod/Supabase) pode lançar fora dos caminhos de
    // erro já tratados — nunca deixa o gateway receber o 500 HTML padrão do
    // Next, que ele não sabe interpretar (só olha status_code + corpo JSON).
    return NextResponse.json(
      { error: e instanceof Error ? e.message : String(e) },
      { status: 502 }
    )
  }
  if ("error" in result) {
    return NextResponse.json({ error: result.error }, { status: statusForError(result.error) })
  }

  return NextResponse.json({
    machine_id: result.machineId,
    name: result.name,
    status: "creating",
    public_url: result.publicUrl,
  })
}

function statusForError(message: string): number {
  if (/^Nenhum produto/.test(message)) return 404
  if (/não pertence ao plano informado/.test(message)) return 400
  if (/não tem tipos de GPU configurados/.test(message) || /Nenhuma GPU/.test(message)) return 422
  return 502
}
