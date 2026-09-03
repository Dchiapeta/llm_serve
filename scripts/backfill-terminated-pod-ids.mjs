/**
 * Backfill de uso único: zera runpod_pod_id/public_url das máquinas que já
 * estão 'terminated'.
 *
 *     node --env-file=.env scripts/backfill-terminated-pod-ids.mjs          # dry-run
 *     node --env-file=.env scripts/backfill-terminated-pod-ids.mjs --apply
 *
 * ---------------------------------------------------------------------------
 * Por que ele precisa rodar ANTES do deploy do gateway
 * ---------------------------------------------------------------------------
 * O gateway passou a recriar sozinho uma máquina PERDIDA quando chega
 * requisição e não há mais nada no plano para servir (resolve_base_machine →
 * try_recreate_machine). Quem decide se ela foi perdida é
 * `recovery.machine_was_lost`, e o sinal é o `runpod_pod_id`:
 *
 *   terminated COM pod_id  → o pod sumiu do RunPod        → recria
 *   terminated SEM pod_id  → alguém clicou em apagar      → não recria
 *
 * `terminateMachine` (lib/actions.ts) agora zera o campo junto com o status, o
 * que torna o sinal confiável DAQUI PRA FRENTE. Mas as máquinas apagadas ANTES
 * dessa mudança ficaram com o pod_id preenchido — e `terminateMachine` também
 * não limpa `stacks.machine_id`. Uma stack que ainda aponte para uma delas
 * faria o gateway novo subir de volta uma GPU que o usuário apagou, na primeira
 * requisição que chegasse.
 *
 * Retroativamente não há como separar "apagada" de "sumiu": os dois casos
 * deixaram exatamente a mesma linha no banco. Por isso o backfill zera TODAS as
 * terminated. O lado conservador é não recriar — uma máquina que de fato sumiu
 * e deixar de ser recriada automaticamente é recuperável à mão; criar uma GPU
 * que ninguém pediu, não.
 *
 * Idempotente: rodar de novo não encontra nada. Não apaga linha nenhuma — só
 * limpa dois campos que já não apontam para nada.
 */
import { createClient } from "@supabase/supabase-js"

const APPLY = process.argv.includes("--apply")
const db = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL,
  process.env.SUPABASE_SERVICE_ROLE_KEY,
  { auth: { persistSession: false } }
)

const { data: alvos, error } = await db
  .from("machines")
  .select("id, name, status, runpod_pod_id, public_url")
  .eq("status", "terminated")
  .not("runpod_pod_id", "is", null)

if (error) {
  console.error("falha ao listar máquinas:", error)
  process.exit(1)
}

if (!alvos.length) {
  console.log("nada a fazer: nenhuma máquina terminated com runpod_pod_id.")
  process.exit(0)
}

console.log(`${alvos.length} máquina(s) terminated com runpod_pod_id:`)
for (const m of alvos) {
  console.log(`  ${m.id}  ${m.name ?? "(sem nome)"}  pod=${m.runpod_pod_id}`)
}

// Quais delas ainda são a "casa" de alguma stack — são exatamente as que o
// gateway novo poderia ressuscitar. Só informativo: o backfill limpa todas.
const { data: stacks } = await db
  .from("stacks")
  .select("id, slug, machine_id")
  .in("machine_id", alvos.map((m) => m.id))

if (stacks?.length) {
  console.log(`\nATENÇÃO: ${stacks.length} stack(s) ainda apontam para essas máquinas —`)
  console.log("são as que disparariam a recriação sem este backfill:")
  for (const s of stacks) console.log(`  stack ${s.slug ?? s.id} → ${s.machine_id}`)
}

if (!APPLY) {
  console.log("\ndry-run. Rode de novo com --apply para gravar.")
  process.exit(0)
}

const { error: upErr } = await db
  .from("machines")
  .update({ runpod_pod_id: null, public_url: null })
  .eq("status", "terminated")
  .not("runpod_pod_id", "is", null)

if (upErr) {
  console.error("falha ao atualizar:", upErr)
  process.exit(1)
}
console.log(`\n${alvos.length} máquina(s) atualizada(s).`)
