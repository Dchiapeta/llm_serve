import { NextResponse, type NextRequest } from "next/server"

import { requireAdminSession } from "@/lib/auth-admin-server"
import { applyCrmFilters, parseScope, toCsv } from "@/lib/crm"

import { getCrmData, parsePeriod } from "../../../(dashboard)/crm/queries"

export const dynamic = "force-dynamic"

/**
 * Export do CRM em CSV.
 *
 * Route handler (e não server action) para o botão ser um `<a href>` simples:
 * idempotente, funciona sem JS e a URL carrega os mesmos filtros da tela. Reusa
 * getCrmData + applyCrmFilters, então planilha e tabela nunca divergem.
 *
 * Já nasce protegido: o matcher do proxy.ts é uma negative-lookahead que cobre
 * tudo que não está explicitamente excluído, e esta rota não está.
 */
export async function GET(req: NextRequest) {
  await requireAdminSession()

  const sp = req.nextUrl.searchParams
  const period = parsePeriod(sp.get("period"))
  const { rows } = await getCrmData(period)

  const filtered = applyCrmFilters(rows, {
    q: sp.get("q") ?? "",
    plano: sp.get("plano") ?? "",
    cobranca: sp.get("cobranca") ?? "",
    escopo: parseScope(sp.get("escopo")),
  })

  const today = new Date().toISOString().slice(0, 10)

  // BOM na frente: sem ele o Excel em pt-BR lê o UTF-8 como latin-1 e todo
  // acento vira caractere quebrado.
  return new NextResponse(`﻿${toCsv(filtered)}`, {
    headers: {
      "content-type": "text/csv; charset=utf-8",
      "content-disposition": `attachment; filename="crm-${today}.csv"`,
      "cache-control": "no-store",
    },
  })
}
