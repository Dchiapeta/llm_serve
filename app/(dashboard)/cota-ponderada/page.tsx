import { createSupabaseAdmin } from "@/lib/supabase/server"
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
import { formatCycleDate, quotaIsCustom } from "@/lib/request-quota"
import {
  BUCKETS,
  QUOTA_PLANS,
  REPORT_DAYS,
  fetchWeightReport,
} from "@/lib/weighted-quota-report"

export const dynamic = "force-dynamic"

const UPSTREAM_LABEL: Record<string, string> = {
  machine: "Máquina própria",
  openrouter: "OpenRouter",
}

const fmt = (n: number) => n.toLocaleString("pt-BR")
const pct = (n: number) =>
  `${(n * 100).toLocaleString("pt-BR", { maximumFractionDigits: 1 })}%`

// Modo log da request ponderada (migration 0079): o que aconteceria se o
// corte por cota ligasse hoje com peso ceil(tokens_in / 32768). Serve para
// decidir os request_quota_override da transição antes de REQUEST_QUOTA_ENFORCE.
export default async function CotaPonderadaPage() {
  const report = await fetchWeightReport(createSupabaseAdmin())

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold">Cota ponderada</h1>
        <p className="text-sm text-muted-foreground">
          Simulação da cota com a request ponderada: até 32K tokens de entrada
          conta 1, acima disso conta ceil(tokens_in / 32.768). Nada é cortado
          enquanto REQUEST_QUOTA_ENFORCE estiver desligado.
        </p>
      </div>

      {!report.available && (
        <Card>
          <CardHeader>
            <CardTitle>Migration 0079 pendente</CardTitle>
            <CardDescription>
              O banco ainda não devolve o uso ponderado
              {report.error ? ` (${report.error})` : ""}. Os números abaixo
              usam a contagem crua até a migration ser aplicada.
            </CardDescription>
          </CardHeader>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Quem bateria 100% da franquia</CardTitle>
          <CardDescription>
            Ciclo atual de cada stack. Projeção = uso até agora estendido até o
            fim do ciclo, só para stacks com 3 dias ou mais de ciclo.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Plano</TableHead>
                <TableHead className="text-right">Stacks</TableHead>
                <TableHead className="text-right">Uso cru</TableHead>
                <TableHead className="text-right">Uso ponderado</TableHead>
                <TableHead className="text-right">Já em 100% (cru → ponderado)</TableHead>
                <TableHead className="text-right">
                  Projetam 100% (cru → ponderado)
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {report.plans.map((p) => (
                <TableRow key={p.plan}>
                  <TableCell className="font-medium">{p.plan}</TableCell>
                  <TableCell className="text-right">{fmt(p.stacks)}</TableCell>
                  <TableCell className="text-right">{fmt(p.raw)}</TableCell>
                  <TableCell className="text-right">
                    {fmt(p.weighted)}
                    {p.raw > 0 && (
                      <span className="text-muted-foreground">
                        {" "}
                        (×{(p.weighted / p.raw).toLocaleString("pt-BR", {
                          maximumFractionDigits: 2,
                        })})
                      </span>
                    )}
                  </TableCell>
                  <TableCell className="text-right">
                    {fmt(p.hitRawNow)} → <strong>{fmt(p.hitWeightedNow)}</strong>
                  </TableCell>
                  <TableCell className="text-right">
                    {fmt(p.projectedRawHit)} →{" "}
                    <strong>{fmt(p.projectedWeightedHit)}</strong>
                    <span className="text-muted-foreground">
                      {" "}
                      de {fmt(p.projectable)}
                    </span>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Stacks para avaliar override</CardTitle>
          <CardDescription>
            Em 80% ou mais do limite com o peso novo, ou projetando passar dele
            até o fim do ciclo. Candidatas a request_quota_override na transição.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {report.atRisk.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              Nenhuma stack perto do limite com o peso novo.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Stack</TableHead>
                  <TableHead>Conta</TableHead>
                  <TableHead>Plano</TableHead>
                  <TableHead className="text-right">Limite</TableHead>
                  <TableHead className="text-right">Cru</TableHead>
                  <TableHead className="text-right">Ponderado</TableHead>
                  <TableHead className="text-right">Projeção</TableHead>
                  <TableHead className="text-right">Renova</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {report.atRisk.map((r) => (
                  <TableRow key={r.stackId}>
                    <TableCell className="font-mono text-xs">
                      {r.slug ?? r.stackId}
                    </TableCell>
                    <TableCell>{r.accountName || "—"}</TableCell>
                    <TableCell>{r.plan}</TableCell>
                    <TableCell className="text-right">
                      {fmt(r.quota.limit)}
                      {quotaIsCustom(r.quota) && (
                        <span className="text-muted-foreground"> (override)</span>
                      )}
                    </TableCell>
                    <TableCell className="text-right">{fmt(r.quota.used)}</TableCell>
                    <TableCell className="text-right font-medium">
                      {fmt(r.usedWeighted)}{" "}
                      <span className="text-muted-foreground">
                        ({pct(r.usedWeighted / r.quota.limit)})
                      </span>
                    </TableCell>
                    <TableCell className="text-right">
                      {r.projectedWeighted === null
                        ? "—"
                        : `${fmt(r.projectedWeighted)} (${pct(
                            r.projectedWeighted / r.quota.limit
                          )})`}
                    </TableCell>
                    <TableCell className="text-right">
                      {formatCycleDate(r.quota.cycleEnd)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Tokens por requisição</CardTitle>
          <CardDescription>
            Últimos {REPORT_DAYS} dias, stacks de LLM, sem playground. Acima de
            32K a requisição pesa mais de 1.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>tokens_in</TableHead>
                {QUOTA_PLANS.map((plan) => (
                  <TableHead key={plan} className="text-right">
                    {plan}
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {BUCKETS.map((b) => (
                <TableRow key={b.key}>
                  <TableCell>{b.label}</TableCell>
                  {QUOTA_PLANS.map((plan) => {
                    const cells = Object.values(report.distribution[plan])
                    const total = cells.reduce((s, c) => s + c.requests, 0)
                    const n = report.distribution[plan][b.key].requests
                    return (
                      <TableCell key={plan} className="text-right">
                        {fmt(n)}
                        <span className="text-muted-foreground">
                          {" "}
                          ({total > 0 ? pct(n / total) : "—"})
                        </span>
                      </TableCell>
                    )
                  })}
                </TableRow>
              ))}
              <TableRow>
                <TableCell className="font-medium">Acima de 32K</TableCell>
                {QUOTA_PLANS.map((plan) => (
                  <TableCell key={plan} className="text-right font-medium">
                    {pct(report.heavyShare[plan])}
                  </TableCell>
                ))}
              </TableRow>
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Requisições sem tokens_in</CardTitle>
          <CardDescription>
            Sem tokens a requisição pesa 1, mesmo que tenha sido grande. Separado
            por quem atendeu, para ver se as máquinas próprias gravam tokens.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Upstream</TableHead>
                <TableHead className="text-right">Requisições</TableHead>
                <TableHead className="text-right">Sem tokens_in</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {report.missingTokens.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={3} className="text-muted-foreground">
                    Sem requisições no período.
                  </TableCell>
                </TableRow>
              ) : (
                report.missingTokens.map((m) => (
                  <TableRow key={m.upstream}>
                    <TableCell>{UPSTREAM_LABEL[m.upstream] ?? m.upstream}</TableCell>
                    <TableCell className="text-right">{fmt(m.requests)}</TableCell>
                    <TableCell className="text-right">
                      {fmt(m.missing)}{" "}
                      <span className="text-muted-foreground">
                        ({m.requests > 0 ? pct(m.missing / m.requests) : "—"})
                      </span>
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  )
}
