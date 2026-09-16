import type { MachineEvent, ProvisionDecision, TriggerEnvelope } from "@/lib/types"
import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { ActorBadge } from "@/components/machines/actor-badge"
import { CauseBadge } from "@/components/machines/cause-badge"
import { actorLabel, isBirthCause } from "@/components/machines/cause-labels"
import { RequestOriginBadge } from "@/components/dashboard/request-origin-badge"

// Aba "Histórico" da página da máquina (migration 0070). Responde em uma tela
// a pergunta que custou uma investigação inteira na llm-stack-505: por que
// esta máquina nasceu, quem disparou e por qual ramo da cascata.

function fmt(ts: string) {
  return new Date(ts).toLocaleString("pt-BR", { timeZone: "America/Sao_Paulo" })
}

// Cor da bolinha da timeline por tipo — o card do dashboard usa verde fixo
// até para erro; aqui o tipo conta.
const DOT: Record<string, string> = {
  created: "bg-emerald-500",
  started: "bg-emerald-500",
  recreated: "bg-amber-500",
  stopped: "bg-zinc-400",
  terminated: "bg-red-500",
  error: "bg-red-500",
}

function Originator({ trigger, actor }: { trigger: TriggerEnvelope | null; actor: string | null }) {
  const t = trigger ?? {}
  const who = actor ?? t.actor ?? null
  if (who === "request") {
    return (
      <dl className="grid grid-cols-[140px_1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-muted-foreground">Conta</dt>
        <dd>{t.account_name ?? t.account_id ?? "—"}</dd>
        <dt className="text-muted-foreground">Stack</dt>
        <dd>
          {t.stack_slug ?? t.stack_id ?? "—"}
          {t.plan && <span className="text-muted-foreground"> · {t.plan}</span>}
          {t.category && <span className="text-muted-foreground"> / {t.category}</span>}
        </dd>
        <dt className="text-muted-foreground">Chave</dt>
        <dd className="font-mono text-xs">{t.key_prefix ? `${t.key_prefix}…` : t.api_key_id ?? "—"}</dd>
        <dt className="text-muted-foreground">Cliente</dt>
        <dd>
          {t.path ? (
            <RequestOriginBadge path={t.path} userAgent={t.user_agent ?? null} />
          ) : (
            <span className="text-muted-foreground">—</span>
          )}
          {t.path && <code className="ml-2 font-mono text-xs text-muted-foreground">{t.path}</code>}
        </dd>
        {t.reason && (
          <>
            <dt className="text-muted-foreground">Motivo</dt>
            <dd className="text-muted-foreground">{t.reason}</dd>
          </>
        )}
      </dl>
    )
  }
  if (who === "admin") {
    return (
      <p className="text-sm">
        Ação manual de <span className="font-medium">{t.admin_email ?? "admin"}</span>
        {t.reason && <span className="text-muted-foreground"> · {t.reason}</span>}
      </p>
    )
  }
  return (
    <p className="text-sm">
      {actorLabel(who)}
      {t.reason && <span className="text-muted-foreground"> · {t.reason}</span>}
    </p>
  )
}

export function MachineHistory({
  events,
  decisions,
}: {
  events: MachineEvent[]
  decisions: ProvisionDecision[]
}) {
  // primeiro evento de nascimento com causa estruturada — o evento `created`
  // é gravado duas vezes (painel + gateway), de propósito; qualquer um serve
  const birth = events.find((e) => isBirthCause(e.cause))

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Por que esta máquina subiu</CardTitle>
          <CardDescription>
            {birth
              ? fmt(birth.created_at)
              : "Sem causa estruturada — máquina anterior à migration 0070 ou evento perdido"}
          </CardDescription>
        </CardHeader>
        {birth && (
          <CardContent className="flex flex-col gap-3">
            <div className="flex flex-wrap items-center gap-2">
              <ActorBadge
                actor={birth.actor ?? birth.trigger_meta?.actor}
                email={birth.trigger_meta?.admin_email}
              />
              <CauseBadge cause={birth.cause} />
            </div>
            <Originator trigger={birth.trigger_meta} actor={birth.actor} />
            {birth.trace_id && (
              <p className="text-xs text-muted-foreground">
                trace_id{" "}
                <code className="select-all font-mono">{birth.trace_id}</code>
                {" "}— mesmo valor do header X-Stac-Request-Id que o cliente recebeu
              </p>
            )}
          </CardContent>
        )}
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Linha do tempo</CardTitle>
          <CardDescription>{events.length} evento(s) desta máquina</CardDescription>
        </CardHeader>
        <CardContent>
          {events.length === 0 ? (
            <p className="text-sm text-muted-foreground">Nenhum evento.</p>
          ) : (
            <ol className="flex flex-col gap-3">
              {events.map((e) => (
                <li key={e.id} className="flex items-start gap-3">
                  <span
                    className={`mt-1.5 size-2 shrink-0 rounded-full ${DOT[e.type] ?? "bg-zinc-400"}`}
                  />
                  <div className="flex min-w-0 flex-1 flex-col gap-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm">{e.message}</span>
                      <Badge variant="outline">{e.type}</Badge>
                      {(e.actor ?? e.trigger_meta?.actor) && (
                        <ActorBadge
                          actor={e.actor ?? e.trigger_meta?.actor}
                          email={e.trigger_meta?.admin_email}
                        />
                      )}
                      {e.cause && <CauseBadge cause={e.cause} />}
                    </div>
                    <p className="text-xs text-muted-foreground">
                      {fmt(e.created_at)}
                      {e.trigger_meta?.key_prefix && ` · chave ${e.trigger_meta.key_prefix}…`}
                      {e.trigger_meta?.stack_slug && ` · ${e.trigger_meta.stack_slug}`}
                    </p>
                  </div>
                </li>
              ))}
            </ol>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>O que mais estava acontecendo</CardTitle>
          <CardDescription>
            Decisões do gateway no mesmo pool (plano/categoria) em ±10 min da criação —
            inclui os 503 que precederam o nascimento e as tentativas negadas
          </CardDescription>
        </CardHeader>
        <CardContent>
          {decisions.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              Nenhuma decisão registrada nessa janela.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Hora</TableHead>
                  <TableHead>Tipo</TableHead>
                  <TableHead>Resultado</TableHead>
                  <TableHead>Causa</TableHead>
                  <TableHead>Stack</TableHead>
                  <TableHead>Chave</TableHead>
                  <TableHead>Origem</TableHead>
                  <TableHead>Motivo</TableHead>
                  <TableHead className="text-right!">Repetições</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {decisions.map((d) => (
                  <TableRow key={d.id}>
                    <TableCell className="text-xs text-muted-foreground">{fmt(d.created_at)}</TableCell>
                    <TableCell>
                      <ActorBadge actor={d.actor} email={d.trigger_meta?.admin_email} />
                    </TableCell>
                    <TableCell>
                      <Badge variant={d.outcome === "granted" ? "secondary" : "destructive"}>
                        {d.outcome}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <CauseBadge cause={d.cause} />
                    </TableCell>
                    <TableCell>{d.trigger_meta?.stack_slug ?? d.stack_id?.slice(0, 8) ?? "—"}</TableCell>
                    <TableCell className="font-mono text-xs">
                      {d.key_prefix ? `${d.key_prefix}…` : "—"}
                    </TableCell>
                    <TableCell>
                      {d.trigger_meta?.path ? (
                        <RequestOriginBadge
                          path={d.trigger_meta.path}
                          userAgent={d.trigger_meta.user_agent ?? null}
                        />
                      ) : (
                        <span className="text-muted-foreground">{actorLabel(d.actor)}</span>
                      )}
                    </TableCell>
                    <TableCell
                      className="max-w-64 truncate text-xs text-muted-foreground"
                      title={d.trigger_meta?.reason ?? undefined}
                    >
                      {d.trigger_meta?.reason ?? "—"}
                    </TableCell>
                    <TableCell className="text-right font-mono text-xs tabular-nums">
                      {d.repeat_count}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
