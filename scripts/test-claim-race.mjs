#!/usr/bin/env node
// Teste de corrida do claim_route: dispara N chamadas RPC concorrentes para a
// mesma conta e assere que exatamente UMA recebe claimed=true (as demais devem
// enxergar o estado 'loading' e recuar). Roda direto contra o Supabase.
//
// Uso:
//   node scripts/test-claim-race.mjs --account-id <uuid> --machine-id <uuid> [--n 10]
//
// Requer no ambiente: NEXT_PUBLIC_SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`)
  if (i === -1 || !process.argv[i + 1]) {
    if (fallback !== undefined) return fallback
    console.error(`Faltou --${name}`)
    process.exit(1)
  }
  return process.argv[i + 1]
}

const ACCOUNT_ID = arg("account-id")
const MACHINE_ID = arg("machine-id")
const N = Number(arg("n", "10"))

const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL
const SERVICE_ROLE = process.env.SUPABASE_SERVICE_ROLE_KEY
if (!SUPABASE_URL || !SERVICE_ROLE) {
  console.error("Defina NEXT_PUBLIC_SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY no ambiente")
  process.exit(1)
}

const headers = {
  apikey: SERVICE_ROLE,
  Authorization: `Bearer ${SERVICE_ROLE}`,
  "Content-Type": "application/json",
}

async function rest(method, path, body) {
  const res = await fetch(`${SUPABASE_URL}/rest/v1${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  const text = await res.text()
  if (!res.ok) throw new Error(`${method} ${path} → ${res.status}: ${text}`)
  return text ? JSON.parse(text) : null
}

// estado limpo: rota da conta volta a 'unloaded'
await rest("PATCH", `/routing_state?account_id=eq.${ACCOUNT_ID}`, {
  machine_id: null,
  lora_status: "unloaded",
})

console.log(`Disparando ${N} claims concorrentes para a conta ${ACCOUNT_ID}…`)
const results = await Promise.all(
  Array.from({ length: N }, () =>
    rest("POST", "/rpc/claim_route", {
      p_account_id: ACCOUNT_ID,
      p_machine_id: MACHINE_ID,
    }).then((rows) => rows[0])
  )
)

const winners = results.filter((r) => r.claimed === true)
const losers = results.filter((r) => r.claimed === false)
console.log(`claimed=true: ${winners.length} · claimed=false: ${losers.length}`)
for (const l of losers.slice(0, 3)) {
  console.log(`  perdedor viu: lora_status=${l.lora_status} machine_id=${l.machine_id}`)
}

// limpeza: devolve a rota para 'unloaded'
await rest("PATCH", `/routing_state?account_id=eq.${ACCOUNT_ID}`, {
  machine_id: null,
  lora_status: "unloaded",
})

if (winners.length !== 1) {
  console.error(`\nFALHOU: esperado exatamente 1 claim vencedor, obtido ${winners.length}`)
  process.exit(1)
}
console.log("\nOK: exatamente 1 vencedor.")
