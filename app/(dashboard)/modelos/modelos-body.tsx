import { getMachinesEnabled, getOpenRouterEnabled } from "@/lib/actions"
import {
  formatPricePerMillion,
  getOpenRouterCatalog,
  type OpenRouterCatalogModel,
} from "@/lib/openrouter"
import { createSupabaseAdmin } from "@/lib/supabase/server"
import type { OpenRouterModel } from "@/lib/types"
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
import { OpenRouterToggle } from "@/components/openrouter/openrouter-toggle"
import {
  OpenRouterModelEnabledSwitch,
  OpenRouterModelRowActions,
} from "@/components/openrouter/openrouter-model-row-actions"

// Corpo da página /modelos: interruptor do repasse + allowlist. Preço e
// contexto vêm do catálogo público do OpenRouter (cache de 1h); se ele estiver
// fora do ar a tabela aparece sem essas colunas, nunca quebra a página.
export async function ModelosBody() {
  const db = createSupabaseAdmin()
  const [{ data }, openRouterEnabled, machinesEnabled, catalog] = await Promise.all([
    db.from("openrouter_models").select("*").order("created_at", { ascending: true }),
    getOpenRouterEnabled(),
    getMachinesEnabled(),
    getOpenRouterCatalog().catch(() => null),
  ])
  const models = (data ?? []) as OpenRouterModel[]
  const active = models.filter((m) => m.enabled).length

  return (
    <div className="flex flex-col gap-6">
      <OpenRouterToggle initialEnabled={openRouterEnabled} activeModels={active} />

      {!machinesEnabled && (
        <p className="text-sm text-muted-foreground">
          As máquinas próprias estão desligadas (página Máquinas): todo texto vai
          para o OpenRouter — o modelo pedido se está na lista, senão o Reserva.
        </p>
      )}
      {catalog === null && (
        <p className="text-sm text-destructive">
          Não foi possível consultar o catálogo do OpenRouter — preço e contexto
          indisponíveis agora.
        </p>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Modelos aceitos</CardTitle>
          <CardDescription>
            {models.length} na lista · {active} ativo(s). O cliente escolhe pelo
            ID, no campo &quot;model&quot; da requisição. Com as máquinas ligadas,
            o OpenRouter só responde quando não há máquina disponível para o
            plano (a máquina é ligada em paralelo); aí responde o modelo pedido
            se está na lista, senão o <strong>Reserva</strong>. Modelos de imagem
            da lista vão sempre para o OpenRouter, sem ligar máquina de imagem.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Modelo</TableHead>
                <TableHead>Tipo</TableHead>
                <TableHead>Preço (1M tokens, entrada / saída)</TableHead>
                <TableHead>Contexto</TableHead>
                <TableHead>Ativo</TableHead>
                <TableHead className="w-12" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {models.length === 0 && (
                <TableRow>
                  <TableCell colSpan={6} className="text-center text-muted-foreground">
                    Nenhum modelo ainda. Adicione o primeiro.
                  </TableCell>
                </TableRow>
              )}
              {models.map((m) => {
                const info: OpenRouterCatalogModel | undefined = catalog?.get(m.slug)
                const missing = catalog !== null && !info
                return (
                  <TableRow key={m.id}>
                    <TableCell>
                      <div className="flex flex-col">
                        <span className="flex items-center gap-2 font-medium">
                          {m.label ?? m.slug}
                          {m.fallback && <Badge variant="secondary">Reserva</Badge>}
                        </span>
                        <code className="font-mono text-xs text-muted-foreground">
                          {m.slug}
                        </code>
                        {missing && (
                          <span className="text-xs text-destructive">
                            Não existe mais no catálogo do OpenRouter
                          </span>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>
                      <Badge variant={m.kind === "image" ? "secondary" : "outline"}>
                        {m.kind === "image" ? "Imagem" : "Texto"}
                      </Badge>
                    </TableCell>
                    <TableCell className="tabular-nums">
                      {formatPricePerMillion(info) ?? "—"}
                    </TableCell>
                    <TableCell className="tabular-nums">
                      {info?.context_length
                        ? `${Math.round(info.context_length / 1000)}K`
                        : "—"}
                    </TableCell>
                    <TableCell>
                      <OpenRouterModelEnabledSwitch model={m} />
                    </TableCell>
                    <TableCell>
                      <OpenRouterModelRowActions model={m} />
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  )
}
