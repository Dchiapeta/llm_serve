import { createClient } from "@supabase/supabase-js"

const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL
const SERVICE_ROLE = process.env.SUPABASE_SERVICE_ROLE_KEY
const db = createClient(SUPABASE_URL, SERVICE_ROLE, { auth: { persistSession: false } })

const { data, error } = await db
  .from("templates")
  .select("id, name, plan, runpod_template_id, image, start_command, created_at")
  .in("plan", ["Pro", "Go"])
  .order("plan", { ascending: true })
  .order("created_at", { ascending: true })

if (error) { console.error(error); process.exit(1) }
console.log(JSON.stringify(data, null, 2))
