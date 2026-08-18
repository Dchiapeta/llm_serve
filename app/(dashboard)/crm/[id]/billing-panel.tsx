import { ExternalLink } from "lucide-react"

import { listInvoicesForCustomer } from "@/lib/chargefy"
import { formatMoney } from "@/lib/chargefy-format"
import { formatDate } from "@/lib/crm"
import { Badge } from "@/components/reui/badge"
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

const INVOICE_BADGE: Record<
  string,
  { label: string; variant: "success-light" | "warning-light" | "destructive-light" | "outline" }
> = {
  paid: { label: "Paga", variant: "success-light" },
  open: { label: "Em aberto", variant: "warning-light" },
  draft: { label: "Rascunho", variant: "outline" },
  uncollectible: { label: "Incobrável", variant: "destructive-light" },
  void: { label: "Anulada", variant: "outline" },
}

/**
 * Painel de faturas. Isolado num Suspense próprio na página de detalhe: uma
 * lentidão da Chargefy atrasa só este bloco, não o perfil inteiro do cliente.
 */
export async function BillingPanel({ customerId }: { customerId: string }) {
  const { ok, error, invoices } = await listInvoicesForCustomer(customerId)

  if (!ok) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Faturas</CardTitle>
          <CardDescription>
            {error ?? "Chargefy não configurada — faturas indisponíveis."}
          </CardDescription>
        </CardHeader>
      </Card>
    )
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Faturas</CardTitle>
        <CardDescription>{invoices.length} fatura(s) na Chargefy</CardDescription>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Número</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Motivo</TableHead>
              <TableHead className="text-right!">Total</TableHead>
              <TableHead className="text-right!">Pago</TableHead>
              <TableHead className="text-right!">Em aberto</TableHead>
              <TableHead>Pagamento</TableHead>
              <TableHead className="w-20" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {invoices.length === 0 && (
              <TableRow>
                <TableCell colSpan={8} className="text-center text-muted-foreground">
                  Nenhuma fatura emitida.
                </TableCell>
              </TableRow>
            )}
            {invoices.map((inv) => {
              const badge = INVOICE_BADGE[inv.status] ?? {
                label: inv.status,
                variant: "outline" as const,
              }
              return (
                <TableRow key={inv.id}>
                  <TableCell className="font-mono text-xs">
                    {inv.number ?? inv.id}
                  </TableCell>
                  <TableCell>
                    <Badge variant={badge.variant}>{badge.label}</Badge>
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {inv.billing_reason ?? "—"}
                  </TableCell>
                  <TableCell className="text-right font-mono text-sm tabular-nums">
                    {formatMoney(inv.amount_total, inv.currency.toUpperCase())}
                  </TableCell>
                  <TableCell className="text-right font-mono text-sm tabular-nums">
                    {formatMoney(inv.amount_paid, inv.currency.toUpperCase())}
                  </TableCell>
                  <TableCell className="text-right font-mono text-sm tabular-nums text-muted-foreground">
                    {formatMoney(inv.amount_remaining, inv.currency.toUpperCase())}
                  </TableCell>
                  <TableCell className="text-sm">
                    {inv.paid_at
                      ? formatDate(inv.paid_at)
                      : inv.due_date
                        ? `vence ${formatDate(inv.due_date)}`
                        : "—"}
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center gap-2">
                      {inv.hosted_invoice_url && (
                        <a
                          href={inv.hosted_invoice_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-muted-foreground hover:text-foreground"
                          title="Abrir fatura"
                        >
                          <ExternalLink className="size-3.5" />
                        </a>
                      )}
                      {inv.invoice_pdf_url && (
                        <a
                          href={inv.invoice_pdf_url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-xs text-muted-foreground hover:text-foreground"
                        >
                          PDF
                        </a>
                      )}
                    </div>
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>

        {invoices.some((i) => i.amount_discount > 0) && (
          <p className="mt-4 text-xs text-muted-foreground">
            Há faturas com desconto aplicado — o valor pago pode ser menor que o
            preço de tabela do plano, ou zero, sem que a assinatura esteja
            inadimplente.
          </p>
        )}
      </CardContent>
    </Card>
  )
}
