"use server"

import { revalidatePath } from "next/cache"

import { flushGatewayKeyCache } from "@/lib/actions"
import { requireAdminSession } from "@/lib/auth-admin-server"
import { createSupabaseAdmin } from "@/lib/supabase/server"

export async function updateStackGenerationConfig(formData: FormData) {
  await requireAdminSession()
  const stackId = formData.get("stack_id")
  const thinking = formData.get("thinking")
  if (typeof stackId !== "string" || !stackId) throw new Error("Stack não informada")
  if (thinking !== "inherit" && thinking !== "on" && thinking !== "off") {
    throw new Error("Configuração de raciocínio inválida")
  }
  const db = createSupabaseAdmin()
  const { data, error } = await db.from("stacks").update({
    system_prompt: String(formData.get("system_prompt") || "").trim() || null,
    default_enable_thinking: thinking === "inherit" ? null : thinking === "on",
  }).eq("id", stackId).select("id").maybeSingle()
  if (error) throw new Error(error.message)
  if (!data) throw new Error("Stack não encontrada")
  await flushGatewayKeyCache().catch((error) =>
    console.error("Configuração salva; cache será renovado pelo TTL:", error)
  )
  revalidatePath("/stacks")
}
