import type { ReactNode } from "react"
import Link from "next/link"
import { Clock } from "lucide-react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

// Documentação do repasse ao OpenRouter (migrations 0071–0074 e 0078). Existe para
// não esquecer que isto está no ar: é um arranjo TEMPORÁRIO, e a seção "Como
// remover" é o checklist do dia em que ele sair.

function Section({
  title,
  description,
  children,
}: {
  title: string
  description?: string
  children: ReactNode
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        {description && <CardDescription>{description}</CardDescription>}
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-sm text-muted-foreground [&_code]:rounded [&_code]:bg-muted [&_code]:px-1.5 [&_code]:py-0.5 [&_code]:text-xs [&_code]:text-foreground">
        {children}
      </CardContent>
    </Card>
  )
}

function List({ children }: { children: ReactNode }) {
  return (
    <ul className="list-disc space-y-1.5 pl-5 marker:text-muted-foreground">{children}</ul>
  )
}

function Strong({ children }: { children: ReactNode }) {
  return <span className="font-medium text-foreground">{children}</span>
}

const ROUTING = [
  {
    when: "Máquina do plano disponível",
    who: "A máquina, qualquer que seja o modelo pedido. A resposta mostra o nome que o cliente pediu.",
  },
  {
    when: "Sem máquina (pausada, subindo, sendo criada)",
    who: "OpenRouter — e a máquina do plano é ligada/criada em paralelo.",
  },
  { when: "Máquina lotada (seria 429)", who: "OpenRouter." },
  {
    when: "Máquina inalcançável ou respondendo 502/503/504 antes do 1º byte",
    who: "OpenRouter.",
  },
  {
    when: "Conversa maior que a janela da máquina",
    who: "OpenRouter (evita matar a sessão do Claude Code), se ainda couber na janela do plano.",
  },
  {
    when: "Conversa maior que a janela do plano (Go 32K, Pro 256K)",
    who: "Ninguém: 400 de contexto, com ou sem máquina.",
  },
]

export default function OpenRouterDocPage() {
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-2">
          <h1 className="text-2xl font-semibold">Repasse ao OpenRouter</h1>
          <Badge variant="outline" className="gap-1">
            <Clock className="size-3" /> Temporário
          </Badge>
        </div>
        <p className="text-sm text-muted-foreground">
          O que existe, como decide para onde vai cada requisição e como tirar
          tudo quando não for mais necessário.{" "}
          <Link href="/documentacao" className="underline underline-offset-4">
            Voltar à documentação
          </Link>
        </p>
      </div>

      <Alert>
        <Clock />
        <AlertTitle>Isto é provisório</AlertTitle>
        <AlertDescription>
          Criado entre 28 e 30/09/2026 para reduzir custo de GPU enquanto as
          máquinas próprias não dão conta sozinhas. A ideia é remover quando as
          máquinas voltarem a ser o único caminho — o checklist está no fim desta
          página.
        </AlertDescription>
      </Alert>

      <Section
        title="Os dois interruptores"
        description="Tabela system_settings; o gateway lê com cache de 30s, e o painel pede para ele esquecer o cache a cada clique."
      >
        <List>
          <li>
            <Strong>Máquinas próprias (RunPod)</Strong> — card na página{" "}
            <Link href="/machines" className="underline underline-offset-4">Máquinas</Link>{" "}
            (<code>machines_enabled</code>). Desligado: nenhuma requisição vai
            para máquina e nada liga GPU sozinho — wake, provisionamento (até o
            disparado por requisição) e recriação são negados. Máquinas já
            ligadas não param por este botão: pausam sozinhas por ociosidade.
          </li>
          <li>
            <Strong>Repasse ao OpenRouter</Strong> — página{" "}
            <Link href="/modelos" className="underline underline-offset-4">Modelos (OpenRouter)</Link>{" "}
            (<code>openrouter_enabled</code>). Ligar também cria no OpenRouter a
            chave de cada chave da Stac que ainda não tem uma.
          </li>
        </List>
      </Section>

      <Section
        title="Para onde vai cada requisição de texto"
        description="Chat (/v1/chat/completions), Claude Code (/v1/messages) e Codex (/v1/responses)."
      >
        <p>
          <Strong>Máquinas ligadas + repasse ligado</Strong> — a máquina é o
          caminho principal e o OpenRouter é o reserva:
        </p>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Situação</TableHead>
              <TableHead>Quem responde</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {ROUTING.map((r) => (
              <TableRow key={r.when}>
                <TableCell className="font-medium text-foreground">{r.when}</TableCell>
                <TableCell className="whitespace-normal">{r.who}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        <p>
          No OpenRouter responde o <Strong>modelo pedido</Strong>, se ele está
          ativo na lista da página Modelos para o plano da chave; senão, o{" "}
          <Strong>reserva de texto do plano</Strong> (hoje{" "}
          <code>qwen/qwen3.5-9b</code> no Go e <code>qwen/qwen3.8-27b</code> no
          Pro — o mesmo modelo da máquina de cada um; trocável na página
          Modelos). É assim que <code>go-base</code>/<code>pro-base</code>, nomes
          que só existem nas máquinas, são atendidos. Assim que a máquina fica
          pronta, a requisição seguinte volta para ela.
        </p>
        <List>
          <li>
            <Strong>Máquinas desligadas + repasse ligado</Strong>: todo texto vai
            direto para o OpenRouter, pela mesma escolha de modelo.
          </li>
          <li>
            <Strong>Repasse desligado</Strong>: comportamento de antes — só
            máquinas.
          </li>
          <li>
            <Strong>Tudo desligado</Strong>: 503.
          </li>
          <li>
            <Strong>Sem reserva e modelo fora da lista</Strong>: com máquinas
            ligadas, o erro da máquina de sempre; com elas desligadas, 404 com a
            lista de modelos aceitos.
          </li>
        </List>
      </Section>

      <Section
        title="Modelos e contexto por plano"
        description="O que o site do TryStac anuncia, aplicado pelo gateway desde 01/10/2026 (migration 0074)."
      >
        <List>
          <li>
            <Strong>Modelos</Strong>: coluna Planos da página{" "}
            <Link href="/modelos" className="underline underline-offset-4">Modelos</Link>{" "}
            (<code>openrouter_models.plans</code>). Go: Qwen 3.5 9B e GLM 5.3
            Flash. Pro: tudo do Go + Qwen 3.8 27B. Pedir um modelo da lista fora
            do plano dá <Strong>403</Strong> com a lista do que o plano tem —
            com máquina ou sem, repasse ligado ou não (a máquina responde
            qualquer nome, então sem a trava o Go receberia o 9B com o nome do
            27B). Nomes fora da lista (<code>go-base</code>, <code>gpt-4o</code>)
            seguem para a máquina como sempre.
          </li>
          <li>
            <Strong>Modelo ainda não lançado</Strong>: fica na lista,{" "}
            <Strong>Ativo</Strong> e sem plano nenhum — 403 &quot;ainda não está
            disponível&quot; para todos. É o caso do Kimi K3 (
            <code>moonshotai/kimi-k3</code>). Desligar o Ativo dele{" "}
            <Strong>tira</Strong> o bloqueio: o gateway só lê os ativos, e fora
            da lista a máquina responderia pelo nome do Kimi. Para lançar, marque
            os planos.
          </li>
          <li>
            <Strong>Contexto</Strong>: Go 32K, Pro 256K, entrada + saída
            (<code>docker/gateway/plan_limits.py</code>, envs{" "}
            <code>PLAN_CONTEXT_TOKENS_GO</code>/<code>_PRO</code>). Na máquina
            vale o menor entre a janela dela e a do plano; no OpenRouter, a do
            plano. O Pro passa dos 128K da máquina pelo desvio ao OpenRouter.
            Max e Enterprise não têm teto próprio.
          </li>
          <li>
            Fora da regra: <code>/v1/documents/*</code> e{" "}
            <code>/v1/images/extract</code> têm os limites próprios por plano
            (páginas, MB), não o teto de contexto.
          </li>
        </List>
      </Section>

      <Section title="Imagem fica fora da regra de reserva">
        <List>
          <li>
            Modelo de imagem ativo na lista vai <Strong>sempre</Strong> direto
            para o OpenRouter — e nenhuma máquina de imagem é ligada ou criada.
          </li>
          <li>Qualquer outro modelo de imagem segue para a máquina de imagem, como sempre.</li>
          <li>Não existe reserva de imagem.</li>
        </List>
      </Section>

      <Section title="Modelos de decisão (Jev)">
        <List>
          <li>
            Modelos que geram <Strong>decisions</Strong> no catálogo do
            OpenRouter (<code>typesafe/jev-1.13</code>) não respondem chat: o
            cliente manda um <code>state</code> e <code>questions</code>{" "}
            tipadas (<code>choice</code>, <code>noul</code>,{" "}
            <code>score</code>) e recebe <code>answers</code> com
            probabilidades.
          </li>
          <li>
            Rota própria: <code>POST /v1/decisions</code>, repassada sem
            tradução ao <code>/api/alpha/decisions</code> do OpenRouter (API
            alpha; o destino muda pela env <code>OPENROUTER_DECISIONS_URL</code>{" "}
            sem deploy de código).
          </li>
          <li>
            Só OpenRouter: nenhuma máquina serve decisão, não existe reserva, e
            modelo fora da lista do plano é 404 com os aceitos. Um modelo de
            decisão também não atende o chat.
          </li>
        </List>
      </Section>

      <Section title="Uma chave no OpenRouter para cada chave da Stac">
        <List>
          <li>
            Nome no OpenRouter: <code>{"<id da conta>/<id da chave>"}</code>. O
            custo por chave aparece na página <Strong>Activity</Strong> do
            OpenRouter, agrupando por API key. Sem teto de gasto — só
            acompanhamento.
          </li>
          <li>
            Nasce quando a chave é criada (manager ou TryStac — os dois passam
            por <code>createKey</code>), ao ligar o repasse (para as que
            faltarem) ou na primeira requisição dela que for para o OpenRouter.
          </li>
          <li>
            O segredo fica cifrado na tabela <code>openrouter_keys</code> (fora
            de <code>api_keys</code>, que o cliente do TryStac lê).
          </li>
          <li>
            Revogar ou excluir no TryStac <Strong>não</Strong> desativa a chave
            no OpenRouter. É inofensivo (o gateway barra antes), mas ela continua
            listada lá.
          </li>
          <li>
            Diagnóstico: <code>POST /admin/openrouter-keys/provision</code> no
            gateway com <code>{`{"api_key_id": "..."}`}</code> responde{" "}
            <code>{`{"ok":true}`}</code> ou o motivo exato (variável faltando,
            repasse desligado...).
          </li>
        </List>
      </Section>

      <Section title="Custos e registros">
        <List>
          <li>
            Toda requisição repassada vai para <Strong>Requisições</Strong>{" "}
            (<code>gateway_requests</code>) com <code>upstream = openrouter</code>{" "}
            e o custo em <code>cost_usd</code>.
          </li>
          <li>
            O custo é <Strong>retirado</Strong> da resposta antes de chegar ao
            cliente — é o que a Stac paga.
          </li>
          <li>
            Chave inválida ou crédito esgotado no OpenRouter viram 503 genérico
            para o cliente; o motivo real fica só no log do gateway.
          </li>
          <li>
            O uso repassado entra em <code>usage_metrics</code> (migration 0075)
            com <code>upstream = openrouter</code> e sem máquina, somado por
            hora a partir de <code>gateway_requests</code>: conta no consumo da
            stack/conta (painel e TryStac) e na cota diária. Fica fora das telas
            por máquina, do rateio de custo de GPU do CRM e da classe de uso da
            stack (que decide a divisão da VRAM). Não contam as requisições que
            nem chegaram ao OpenRouter (503) nem as recusadas por problema da
            conta da Stac lá (401/402).
          </li>
        </List>
      </Section>

      <Section title="Limitações conhecidas">
        <List>
          <li>
            <code>/v1/documents/*</code> e <code>/v1/images/extract</code> só
            existem nas máquinas: sem máquina, continuam dando 503.
          </li>
          <li>
            Stack com fine-tune (LoRA): quando cai no OpenRouter, responde o
            modelo base pedido/reserva, sem o fine-tune.
          </li>
          <li>
            No OpenRouter não valem o piso de <code>max_tokens</code>, a
            política de thinking da chave/stack nem os defaults de imagem
            (calibrados para as máquinas). Teto de saída:{" "}
            <code>OPENROUTER_MAX_TOKENS</code> (32000).
          </li>
          <li>
            Codex (<code>/v1/responses</code>) no OpenRouter não recebe o system
            prompt da stack.
          </li>
          <li>
            Máquina lotada também desvia: em pico, o gasto com o OpenRouter sobe
            junto.
          </li>
          <li>
            Privacidade: não há restrição de provedor
            (<code>provider.data_collection</code>) configurada.
          </li>
        </List>
      </Section>

      <Section
        title="Configuração"
        description="Variáveis do serviço do gateway no Railway."
      >
        <List>
          <li>
            <code>OPENROUTER_MANAGEMENT_KEY</code> — chave de{" "}
            <Strong>gerenciamento</Strong> (openrouter.ai/settings/management-keys),
            cria as chaves por usuário.
          </li>
          <li>
            <code>OPENROUTER_KEYS_ENCRYPTION_KEY</code> — senha de cifra dos
            segredos (gerada por nós; trocar invalida as chaves já criadas).
          </li>
          <li>
            <code>OPENROUTER_API_KEY</code> — opcional: chave compartilhada de
            plano B, para quando a chave de um usuário não puder ser criada.
          </li>
          <li>
            Opcionais de ajuste: <code>OPENROUTER_MAX_TOKENS</code>,{" "}
            <code>OPENROUTER_STREAM_TTFT_TIMEOUT_S</code>,{" "}
            <code>OPENROUTER_STREAM_IDLE_TIMEOUT_S</code>,{" "}
            <code>OPENROUTER_NONSTREAM_TIMEOUT_S</code>,{" "}
            <code>OPENROUTER_IMAGE_TIMEOUT_S</code>,{" "}
            <code>OPENROUTER_MAX_IMAGES</code>,{" "}
            <code>OPENROUTER_KEY_RETRY_S</code> (ver o README do gateway).
          </li>
        </List>
      </Section>

      <Section
        title="Como remover"
        description="Checklist para o dia em que as máquinas voltarem a ser o único caminho."
      >
        <ol className="list-decimal space-y-1.5 pl-5 marker:text-muted-foreground">
          <li>
            Desligar o <Strong>Repasse ao OpenRouter</Strong> e confirmar que as{" "}
            <Strong>Máquinas próprias</Strong> estão ligadas. Isso já volta ao
            comportamento antigo sem deploy nenhum.
          </li>
          <li>
            Gateway (<code>docker/gateway</code>): apagar{" "}
            <code>openrouter.py</code>, <code>openrouter_keys.py</code> e os{" "}
            <code>test_openrouter*.py</code>; em <code>main.py</code>, tirar o
            bloco de envs <code>OPENROUTER_*</code>, os clients/caches do
            OpenRouter, <code>_FallbackToOpenRouter</code>,{" "}
            <code>_EchoedUpstream</code>, os ramos de destino no catch-all,{" "}
            <code>/v1/messages</code> e rotas de imagem, e os endpoints{" "}
            <code>/admin/openrouter-keys/*</code>; em <code>supa.py</code>, os
            métodos de <code>openrouter_models</code>/<code>openrouter_keys</code>;{" "}
            <code>cryptography</code> do <code>requirements.txt</code>.
          </li>
          <li>
            Painel: apagar <code>app/(dashboard)/modelos</code>,{" "}
            <code>components/openrouter</code>, <code>lib/openrouter.ts</code>,
            esta página, o item &quot;Modelos (OpenRouter)&quot; do menu, a seção
            OpenRouter de <code>lib/actions.ts</code> e a chamada a{" "}
            <code>provisionOpenRouterKey</code> em <code>createKey</code>.
          </li>
          <li>
            Decidir se o interruptor <Strong>Máquinas próprias</Strong>{" "}
            (<code>machines_enabled</code>) fica — ele é útil por si só como
            corta-custo de emergência.
          </li>
          <li>
            O teto de contexto por plano (<code>plan_limits.py</code>){" "}
            <Strong>não</Strong> é do repasse: fica. Já o acesso a modelo por
            plano mora em <code>openrouter_models.plans</code> e some junto com
            a tabela — com só máquinas, cada plano só tem o modelo da própria
            máquina, então a trava deixa de ser necessária.
          </li>
          <li>
            Banco: migration nova que apaga <code>openrouter_keys</code>,{" "}
            <code>openrouter_models</code> e a linha{" "}
            <code>openrouter_enabled</code> de <code>system_settings</code>.
            Manter <code>gateway_requests.upstream</code>/<code>cost_usd</code>{" "}
            preserva o histórico de custo.
          </li>
          <li>
            OpenRouter: excluir as chaves <code>{"<conta>/<chave>"}</code>{" "}
            (página Keys ou API de gerenciamento) e a chave de gerenciamento.
            Railway: remover as variáveis <code>OPENROUTER_*</code>.
          </li>
        </ol>
      </Section>
    </div>
  )
}
