// PostgreSQL local em memória, sem conexões à nuvem.
// STAC_PGLITE_MODULE=/absolute/path/to/pglite/dist/index.js node test_product_analytics_sql.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
const { PGlite } = await import(process.env.STAC_PGLITE_MODULE || '@electric-sql/pglite');
const db = new PGlite();
await db.exec(`
  create role anon; create role authenticated; create role service_role bypassrls;
  create table accounts (id uuid primary key, user_id uuid, email text);
  create table api_keys (id uuid primary key, purpose text not null default 'customer');
  create table gateway_requests (id uuid primary key default gen_random_uuid(), account_id uuid,
    stack_id uuid, api_key_id uuid, path text, model text, status_code integer, stream boolean default false,
    tokens_in integer, tokens_out integer, duration_ms integer, cost_usd numeric,
    created_at timestamptz default now());
  grant select, insert on accounts, api_keys, gateway_requests to service_role;
`);
const migration = new URL('../../supabase/migrations/20261002142202_posthog_gateway_analytics.sql', import.meta.url);
await db.exec(await fs.readFile(migration, 'utf8'));
const account = '11111111-1111-4111-8111-111111111111';
const user = '22222222-2222-4222-8222-222222222222';
await db.query('insert into accounts values ($1,$2,$3)', [account, user, 'customer@example.test']);
const day = (await db.query("select ((now() at time zone 'UTC' - interval '2 hours')::date - 1)::text as day")).rows[0].day;
await db.query('update gateway_analytics_state set next_day = $1::date', [day]);
async function request(outcome, overrides = {}) {
  const data = { origin: 'customer', path: 'chat/completions', model: 'qwen3.5', duration: 100,
    tokens_in: null, tokens_out: null, ...overrides };
  await db.query(`insert into gateway_requests (account_id,path,model,status_code,analytics_origin,
    analytics_outcome,duration_ms,tokens_in,tokens_out,created_at)
    values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::date + interval '1 hour')`,
    [account, data.path, data.model, outcome === 'completed' ? 200 : 499,
      data.origin, outcome, data.duration, data.tokens_in, data.tokens_out, day]);
}
await request('aborted', { duration: 300 });
await request('error', { duration: 50 });
assert.equal((await db.query('select count(*)::int as n from gateway_analytics_outbox')).rows[0].n, 0);
await request('completed');
await request('completed', { duration: 900, tokens_in: 10, tokens_out: 20 });
await request('completed', { origin: 'playground' });
await request('completed', { path: 'models' });
await request(null, { origin: null });
await request('completed', { path: 'documents/generate' });
const first = (await db.query("select * from gateway_analytics_outbox where event='first_inference_completed'")).rows;
assert.equal(first.length, 1, 'um primeiro uso por conta, não por chamada');
assert.equal(first[0].distinct_id, user);
assert.equal(first[0].properties.$internal_or_test_user, false);
assert.ok(!JSON.stringify(first).includes('customer@example'));
assert.equal(first[0].properties.tokens_in, undefined, 'desconhecido não vira zero no primeiro uso');

await db.exec('begin');
await db.query('select prepare_gateway_analytics_exports()');
await db.exec('rollback');
assert.equal((await db.query('select next_day::text from gateway_analytics_state')).rows[0].next_day, day);
assert.equal((await db.query("select count(*)::int as n from gateway_analytics_outbox where event='api_usage_daily'")).rows[0].n, 0);
await db.exec('set role service_role');
await db.query('select prepare_gateway_analytics_exports()');
await db.query('select prepare_gateway_analytics_exports()');
await db.exec('reset role');
const daily = (await db.query("select * from gateway_analytics_outbox where event='api_usage_daily'")).rows;
assert.equal(daily.length, 1, 'checkpoint não recria a janela');
const p = daily[0].properties;
assert.equal(p.request_count, 4);
assert.equal(p.request_count, p.success_count + p.error_count + p.aborted_count);
assert.equal(p.success_count, 2);
assert.equal(p.tokens_in, 10); assert.equal(p.tokens_out, 20); assert.equal(p.tokens_known_count, 1);
assert.equal(p.duration_ms_sum, 1350); assert.equal(p.duration_known_count, 4);
assert.ok(Math.abs(p.duration_ms_p95 - 810) < 1e-9); // distribuição completa [50,100,300,900]
assert.equal(p.cost_known_count, 0);
assert.equal(p.period_start.slice(0,10), day);
assert.ok(p.period_end > p.period_start);

let claim = (await db.query('select * from claim_gateway_analytics_exports(1,100)')).rows;
assert.equal(claim.length, 1); assert.equal(claim[0].event, 'first_inference_completed');
assert.equal((await db.query('select * from claim_gateway_analytics_exports(1,100)')).rows.length, 0, 'lease/orçamento bloqueia nova entrega');
await db.query("update gateway_analytics_outbox set lease_until=now()-interval '1 minute' where id=$1", [claim[0].id]);
const retry = (await db.query('select * from claim_gateway_analytics_exports(1,100)')).rows;
assert.equal(retry.length, 1); assert.equal(retry[0].id, first[0].id);
for (const key of ['id','event','distinct_id','event_timestamp']) assert.deepEqual(retry[0][key], claim[0][key]);
assert.deepEqual(retry[0].properties, claim[0].properties);
assert.equal((await db.query('select reserved_events from gateway_analytics_state')).rows[0].reserved_events, 1, 'retry não reserva outra unidade');
await db.query('update gateway_analytics_outbox set sent_at=now() where id=$1', [claim[0].id]);
assert.equal((await db.query('select * from claim_gateway_analytics_exports(1,100)')).rows.length, 0);
await db.exec("update gateway_analytics_state set budget_month=date '2000-01-01'");
claim = (await db.query('select * from claim_gateway_analytics_exports(1,100)')).rows;
assert.equal(claim.length, 1); assert.equal(claim[0].event, 'api_usage_daily', 'mês novo retoma backlog');
assert.equal((await db.query('select * from claim_gateway_analytics_exports(0,100)')).rows.length, 0);
assert.equal((await db.query("select has_function_privilege('authenticated','prepare_gateway_analytics_exports()','execute') as allowed")).rows[0].allowed, false);
await db.exec('set role authenticated');
await assert.rejects(db.query('select * from gateway_analytics_outbox'), /permission denied/);
await db.exec('reset role');

// Falha real da outbox não desfaz a inserção de negócio. O job recupera o
// primeiro uso depois, usando o horário durável da primeira linha do ledger.
const recoveryAccount = '44444444-4444-4444-8444-444444444444';
const recoveryUser = '55555555-5555-4555-8555-555555555555';
await db.query('insert into accounts values ($1,$2,$3)', [recoveryAccount, recoveryUser, 'team@trystac.com']);
await db.exec('revoke insert on gateway_analytics_outbox from service_role; set role service_role');
await db.query(`insert into gateway_requests (account_id,path,model,status_code,analytics_origin,analytics_outcome,created_at)
  values ($1,'chat/completions','qwen3.5',200,'customer','completed',$2::date + interval '2 hours')`, [recoveryAccount,day]);
await db.exec('reset role');
assert.equal((await db.query('select count(*)::int as n from gateway_requests where account_id=$1', [recoveryAccount])).rows[0].n, 1);
assert.equal((await db.query("select count(*)::int as n from gateway_analytics_outbox where resource_key=$1", ['first:'+recoveryAccount])).rows[0].n, 0);
await db.exec('grant insert on gateway_analytics_outbox to service_role');
await db.query('update gateway_analytics_state set next_day=$1::date', [day]);
await db.exec('set role service_role');
await db.query('select prepare_gateway_analytics_exports()');
await db.exec('reset role');
const recovered = (await db.query("select * from gateway_analytics_outbox where resource_key=$1", ['first:'+recoveryAccount])).rows[0];
assert.equal(recovered.distinct_id,recoveryUser);
assert.equal(recovered.properties.$internal_or_test_user,true);
assert.equal(new Date(recovered.event_timestamp).toISOString().slice(11,19),'02:00:00');
assert.ok(!JSON.stringify(recovered).includes('team@trystac.com'));

// Retenção só remove summaries já enviados; o checkpoint e dedupe do
// primeiro uso são conservados, inclusive sob a limpeza seguinte.
await db.query("update gateway_analytics_outbox set sent_at=now()-interval '91 days' where id=$1", [first[0].id]);
await db.query(`insert into gateway_analytics_outbox (resource_key,event,distinct_id,event_timestamp,properties,sent_at)
  values ('daily:retention-sent','api_usage_daily',$1,now()-interval '100 days','{}',now()-interval '91 days'),
    ('daily:retention-unsent','api_usage_daily',$1,now()-interval '100 days','{}',null)`, [user]);
const checkpoint = (await db.query('select next_day from gateway_analytics_state')).rows[0].next_day;
await db.query('select prepare_gateway_analytics_exports()');
assert.equal((await db.query("select count(*)::int as n from gateway_analytics_outbox where resource_key='daily:retention-sent'")).rows[0].n,0);
assert.equal((await db.query("select count(*)::int as n from gateway_analytics_outbox where resource_key='daily:retention-unsent'")).rows[0].n,1);
assert.equal((await db.query('select count(*)::int as n from gateway_analytics_outbox where id=$1',[first[0].id])).rows[0].n,1);
assert.deepEqual((await db.query('select next_day from gateway_analytics_state')).rows[0].next_day,checkpoint);
// Conta que já usava a API antes do tracking: só um marcador descartado,
// nunca um falso primeiro uso, e o marcador nunca é reservado/enviado.
const oldAccount = '66666666-6666-4666-8666-666666666666';
const playAccount = '77777777-7777-4777-8777-777777777777';
const playKey = '88888888-8888-4888-8888-888888888888';
await db.query('insert into accounts values ($1,$2,$3),($4,$5,$6)', [oldAccount, user, 'old@example.test',
  playAccount, recoveryUser, 'play@example.test']);
await db.query("insert into api_keys values ($1,'playground')", [playKey]);
await db.query(`insert into gateway_requests (account_id,api_key_id,path,model,status_code,created_at) values
  ($1,null,'chat/completions','qwen3.5',200,now()-interval '30 days'),
  ($2,$3,'chat/completions','qwen3.5',200,now()-interval '30 days'),
  ($2,null,'models',null,200,now()-interval '30 days')`, [oldAccount, playAccount, playKey]);
for (const acc of [oldAccount, playAccount]) {
  await db.exec('set role service_role');
  await db.query(`insert into gateway_requests (account_id,path,model,status_code,analytics_origin,analytics_outcome)
    values ($1,'chat/completions','qwen3.5',200,'customer','completed')`, [acc]);
  await db.exec('reset role');
}
const oldMarker = (await db.query('select * from gateway_analytics_outbox where resource_key=$1', ['first:'+oldAccount])).rows;
assert.equal(oldMarker.length, 1);
assert.equal(oldMarker[0].discard_reason, 'preexisting_usage');
assert.ok(oldMarker[0].discarded_at);
const playFirst = (await db.query('select * from gateway_analytics_outbox where resource_key=$1', ['first:'+playAccount])).rows;
assert.equal(playFirst.length, 1);
assert.equal(playFirst[0].discarded_at, null, 'histórico só de Playground/listagem não é uso de cliente');
// Envelope inválido descartado pelo exportador sai da fila.
await db.query("update gateway_analytics_outbox set discarded_at=now(), discard_reason='invalid_envelope' where id=$1", [playFirst[0].id]);
await db.exec("update gateway_analytics_state set reserved_events=0");
const drained = (await db.query('select * from claim_gateway_analytics_exports(200000,100)')).rows.map((r) => r.id);
assert.ok(!drained.includes(oldMarker[0].id) && !drained.includes(playFirst[0].id), 'descartados nunca são reservados');
await assert.rejects(db.query("update gateway_analytics_outbox set discarded_at=now() where id=$1", [first[0].id]), /check/);

await db.exec("update gateway_analytics_state set budget_month=date_trunc('month',now() at time zone 'UTC')::date,reserved_events=200000");
assert.equal((await db.query('select * from claim_gateway_analytics_exports(2147483647,100)')).rows.length,0,'cap de 200k não aceita override maior');
console.log('PostgreSQL: first-use, privacy, closed windows, checkpoint rollback, stable retry, leases, monthly budget/cap, fail-open recovery, retention, preexisting-usage suppression, discarded rows and service-only permissions passed');
await db.close();
