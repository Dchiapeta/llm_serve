import { cache } from "react"

import { createSupabaseAdmin } from "@/lib/supabase/server"
import type { Template } from "@/lib/types"

// Deduplica a query de templates dentro da mesma request: o toolbar (dialog de
// criar) e o corpo da tabela precisam dela, mas cada um vive no seu <Suspense>.
export const getTemplates = cache(async (): Promise<Template[]> => {
  const db = createSupabaseAdmin()
  const { data } = await db.from("templates").select("*").order("name")
  return (data ?? []) as Template[]
})
