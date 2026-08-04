import type { ReactNode } from "react"

import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion"
import { Card, CardContent } from "@/components/ui/card"
import { ApiReference } from "@/components/documentacao/api-reference"
import { MachineAbout } from "@/components/machines/machine-about"

function Lead({ children }: { children: ReactNode }) {
  return <p className="text-foreground">{children}</p>
}

function List({ children }: { children: ReactNode }) {
  return (
    <ul className="list-disc space-y-1.5 pl-5 marker:text-muted-foreground">
      {children}
    </ul>
  )
}

export default function DocumentacaoPage() {
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold">Documentação</h1>
        <p className="text-sm text-muted-foreground">
          Como a plataforma funciona por baixo do capô: máquinas, modelos,
          imagem docker, planos e o gateway que liga tudo isso.
        </p>
      </div>

      <Card>
        <CardContent>
          <Accordion
            type="multiple"
            defaultValue={["arquitetura"]}
            className="text-sm text-muted-foreground [&_code]:rounded [&_code]:bg-muted [&_code]:px-1.5 [&_code]:py-0.5 [&_code]:text-xs [&_code]:text-foreground"
          >
            <AccordionItem value="arquitetura">
              <AccordionTrigger className="text-base font-medium text-foreground">
                Visão geral da arquitetura
              </AccordionTrigger>
              <AccordionContent>
                <Lead>
                  O sistema tem três peças, cada uma com uma responsabilidade
                  clara:
                </Lead>
                <List>
                  <li>
                    <span className="font-medium text-foreground">
                      Painel (este app)
                    </span>{" "}
                    — Next.js + Supabase. É onde você cria produtos, sobe
                    máquinas, gerencia contas/chaves e acompanha uso e custo.
                  </li>
                  <li>
                    <span className="font-medium text-foreground">
                      Máquinas
                    </span>{" "}
                    — pods do RunPod rodando a imagem docker do projeto:{" "}
                    <code>vLLM</code> interno na porta <code>8001</code> (serve
                    o modelo) + um agent FastAPI na porta <code>8000</code>{" "}
                    (única exposta), que valida chaves HEX, mede uso e expõe
                    logs/métricas ao painel.
                  </li>
                  <li>
                    <span className="font-medium text-foreground">
                      Gateway
                    </span>{" "}
                    — serviço à parte (deploy no Railway), o único endereço
                    que o cliente final conhece. Recebe a chamada, decide em
                    qual máquina atender e faz o proxy, cuidando de
                    religar/pausar/realocar máquinas por baixo dos panos.
                  </li>
                </List>
                <p>
                  Fluxo de uma chamada:{" "}
                  <code>
                    cliente → gateway (:8080) → agent do pod (:8000) → vLLM
                    (:8001)
                  </code>
                  .
                </p>
              </AccordionContent>
            </AccordionItem>

            <AccordionItem value="produtos">
              <AccordionTrigger className="text-base font-medium text-foreground">
                Produtos (templates)
              </AccordionTrigger>
              <AccordionContent>
                <Lead>
                  Um produto define{" "}
                  <span className="font-medium text-foreground">o quê</span>{" "}
                  vai rodar: imagem docker, modelo, GPU(s) e os parâmetros de
                  capacidade usados para calcular quantos usuários cabem numa
                  máquina. Toda máquina nasce a partir de um produto.
                </Lead>
                <List>
                  <li>
                    Imagem docker padrão do projeto:{" "}
                    <code>dchiapeta/vllm-agent:latest</code> (vLLM + agent).
                  </li>
                  <li>
                    O painel injeta automaticamente na máquina, a partir do
                    produto: <code>MODEL_NAME</code>,{" "}
                    <code>AGENT_ADMIN_SECRET</code> e <code>GPU_COUNT</code>{" "}
                    (de <code>gpu_count</code>) — mais de 1 GPU liga{" "}
                    <code>--tensor-parallel-size</code> automaticamente.
                  </li>
                  <li>
                    Outros ajustes do vLLM (ex.: <code>--max-model-len</code>,
                    overrides de HF) vão em <code>VLLM_EXTRA_ARGS</code>,
                    configurável no produto.
                  </li>
                  <li>
                    Footprint de VRAM do modelo e reserva por usuário também
                    são configurados por produto — é o que alimenta o cálculo
                    de slots (ver seção de planos e capacidade).
                  </li>
                </List>
              </AccordionContent>
            </AccordionItem>

            <AccordionItem value="maquinas">
              <AccordionTrigger className="text-base font-medium text-foreground">
                Máquinas: ligar, pausar e apagar
              </AccordionTrigger>
              <AccordionContent>
                <Lead>
                  Cada máquina é um pod do RunPod. O painel expõe as ações do
                  ciclo de vida; por trás, o gateway também liga e pausa
                  máquinas sozinho, para economizar GPU ociosa.
                </Lead>
                <List>
                  <li>
                    <span className="font-medium text-foreground">Criar</span>{" "}
                    — escolhe produto + GPU e sobe o pod no RunPod já com as
                    variáveis de ambiente do produto.
                  </li>
                  <li>
                    <span className="font-medium text-foreground">
                      Iniciar / Pausar
                    </span>{" "}
                    — start/stop do pod. Pausar não apaga a máquina, só
                    desliga a GPU (e zera a VRAM: qualquer adapter carregado
                    se perde). Ao religar, o painel reenvia as chaves ativas
                    ao agent, que sobe sem nenhuma em memória.
                  </li>
                  <li>
                    <span className="font-medium text-foreground">
                      Recriar
                    </span>{" "}
                    — usado como recuperação quando uma máquina fica presa
                    (ex.: host sem GPU livre para religar).
                  </li>
                  <li>
                    <span className="font-medium text-foreground">
                      Apagar
                    </span>{" "}
                    — termina o pod definitivamente no RunPod.
                  </li>
                </List>
                <p className="font-medium text-foreground">
                  Automações do gateway (ninguém precisa clicar em nada):
                </p>
                <List>
                  <li>
                    <span className="font-medium text-foreground">
                      Auto-pausa por ociosidade
                    </span>{" "}
                    — máquina running sem nenhuma atividade por um tempo
                    configurável, sem rotas ativas e sem request em voo, é
                    pausada sozinha (stop no RunPod).
                  </li>
                  <li>
                    <span className="font-medium text-foreground">
                      Auto-wake (religar sozinho)
                    </span>{" "}
                    — chegou uma request e nenhuma máquina running do plano
                    tem vaga: o gateway religa a máquina pausada mais
                    adequada e responde 503 com <code>Retry-After</code>{" "}
                    enquanto o vLLM sobe (esse warm-up leva de ~3 a 8 min).
                  </li>
                  <li>
                    <span className="font-medium text-foreground">
                      Provisionamento automático
                    </span>{" "}
                    — controlado por um interruptor liga/desliga na página de
                    Máquinas (nasce desligado). Quando ligado, se nem uma
                    máquina running com vaga nem uma pausada resolvem, o
                    gateway pede ao próprio painel para criar uma máquina
                    nova; também mantém proativamente uma reserva pausada por
                    plano, para não pagar GPU ociosa à toa.
                  </li>
                  <li>
                    <span className="font-medium text-foreground">
                      Consolidação
                    </span>{" "}
                    — de tempos em tempos, o gateway migra contas de uma
                    máquina pouco usada para outra do mesmo produto com vaga,
                    esvaziando máquinas para poderem ser pausadas.
                  </li>
                </List>
              </AccordionContent>
            </AccordionItem>

            <AccordionItem value="stacks">
              <AccordionTrigger className="text-base font-medium text-foreground">
                Stacks
              </AccordionTrigger>
              <AccordionContent>
                <Lead>
                  Uma stack é a instância de um plano rodando numa máquina
                  específica — é o que amarra "qual conta está em qual
                  máquina, servida por qual plano". Uma máquina pode hospedar
                  vagas (slots) de uma ou mais stacks, e uma stack pode ser
                  migrada de máquina sem trocar a chave do cliente.
                </Lead>
                <List>
                  <li>
                    O plano de uma chave é sempre o da stack a que ela
                    pertence — não existe mais um "plano da conta" solto.
                  </li>
                  <li>
                    Se a máquina da stack pausa ou termina, o gateway realoca
                    automaticamente para outra máquina running do{" "}
                    <span className="font-medium text-foreground">
                      mesmo plano
                    </span>{" "}
                    com vaga; se não há vaga em lugar nenhum, ele religa a
                    própria máquina da stack.
                  </li>
                  <li>
                    Migração de stack (manual, pelo painel, ou automática, na
                    consolidação) nunca corta uma resposta em andamento: a
                    origem continua servindo até o destino confirmar o load.
                  </li>
                </List>
              </AccordionContent>
            </AccordionItem>

            <AccordionItem value="planos-capacidade">
              <AccordionTrigger className="text-base font-medium text-foreground">
                Planos e capacidade (slots)
              </AccordionTrigger>
              <AccordionContent>
                <Lead>
                  Cada máquina tem um número máximo de "vagas" (slots) de
                  usuários simultâneos, calculado a partir da VRAM da GPU e de
                  dois números configurados no produto:
                </Lead>
                <p className="rounded-md bg-muted px-3 py-2 font-mono text-xs text-foreground">
                  slots_max = floor((VRAM da GPU − footprint do modelo) /
                  reserva por usuário)
                </p>
                <List>
                  <li>
                    <span className="font-medium text-foreground">
                      Footprint
                    </span>{" "}
                    — quanta VRAM o modelo consome parado, antes de qualquer
                    usuário.
                  </li>
                  <li>
                    <span className="font-medium text-foreground">
                      Reserva por usuário
                    </span>{" "}
                    — quanta VRAM extra cada usuário simultâneo consome
                    (contexto/KV cache).
                  </li>
                </List>
                <p>
                  Escalar um plano para mais usuários é, na prática, trocar
                  GPU (mais VRAM) ou reduzir a reserva por usuário (ex.:
                  KV cache em fp8) — o princípio adotado hoje é priorizar
                  performance/capacidade do modelo por usuário, aceitando
                  atender menos usuários simultâneos por máquina quando
                  necessário.
                </p>
              </AccordionContent>
            </AccordionItem>

            <AccordionItem value="contas-chaves">
              <AccordionTrigger className="text-base font-medium text-foreground">
                Contas e chaves de acesso
              </AccordionTrigger>
              <AccordionContent>
                <Lead>
                  Cada usuário final recebe uma chave de acesso em formato
                  HEX, atrelada a uma conta e a uma stack.
                </Lead>
                <List>
                  <li>
                    A chave é exibida{" "}
                    <span className="font-medium text-foreground">
                      uma única vez
                    </span>{" "}
                    na criação — o painel guarda só o hash, então ela não
                    pode ser recuperada depois, apenas revogada e substituída
                    por uma nova.
                  </li>
                  <li>
                    Revogar uma chave invalida o acesso quase imediatamente:
                    o painel avisa o gateway para limpar o cache de chaves em
                    memória.
                  </li>
                  <li>
                    Uma conta sem stack resolvida é rejeitada pelo gateway —
                    não existe fallback silencioso para "qualquer plano".
                  </li>
                </List>
              </AccordionContent>
            </AccordionItem>

            <AccordionItem value="gateway-roteamento">
              <AccordionTrigger className="text-base font-medium text-foreground">
                Gateway e roteamento
              </AccordionTrigger>
              <AccordionContent>
                <Lead>
                  O cliente final deve chamar{" "}
                  <span className="font-medium text-foreground">
                    sempre pelo gateway
                  </span>
                  , nunca direto pelo proxy do pod — só o gateway sabe rotear
                  para a máquina certa, religar pods pausados e realocar
                  stacks de forma transparente.
                </Lead>
                <List>
                  <li>
                    O endpoint é compatível com as APIs da OpenAI e da
                    Anthropic (chat completions, streaming SSE).
                  </li>
                  <li>
                    O campo <code>model</code> do request é livre — o gateway
                    reescreve para o modelo real do plano/adapter em toda
                    chamada, então o valor enviado pelo cliente não importa.
                  </li>
                  <li>
                    Autenticação por <code>Authorization: Bearer &lt;chave-hex&gt;</code>,
                    validada por hash com cache em memória no gateway.
                  </li>
                  <li>
                    Se a máquina certa estiver pausada, o gateway religa
                    sozinho e responde 503 com <code>Retry-After</code> — o
                    cliente só precisa dar retry.
                  </li>
                </List>
              </AccordionContent>
            </AccordionItem>

            <AccordionItem value="exemplos-request">
              <AccordionTrigger className="text-base font-medium text-foreground">
                Exemplos de chamada (todas as linguagens)
              </AccordionTrigger>
              <AccordionContent>
                <Lead>
                  Os mesmos exemplos disponíveis na aba "Sobre" de cada
                  máquina, aqui com valores genéricos — troque a URL e o
                  modelo pelos da sua máquina.
                </Lead>
                <MachineAbout gatewayUrl={null} modelName={null} maxModelLen={null} />
              </AccordionContent>
            </AccordionItem>

            <AccordionItem value="referencia-api">
              <AccordionTrigger className="text-base font-medium text-foreground">
                Referência da API por caso de uso
              </AccordionTrigger>
              <AccordionContent>
                <ApiReference />
              </AccordionContent>
            </AccordionItem>

            <AccordionItem value="lora">
              <AccordionTrigger className="text-base font-medium text-foreground">
                Adapters LoRA
              </AccordionTrigger>
              <AccordionContent>
                <Lead>
                  Contas podem ter um adapter LoRA próprio, carregado
                  dinamicamente sobre o modelo base — o treino do adapter
                  acontece fora deste sistema; o painel só registra e
                  gerencia adapters já prontos.
                </Lead>
                <List>
                  <li>
                    Convenção de path no bucket privado <code>loras</code> do
                    Supabase Storage:{" "}
                    <code>
                      loras/&#123;account_id&#125;/&#123;version&#125;/adapter_config.json
                    </code>{" "}
                    e{" "}
                    <code>
                      loras/&#123;account_id&#125;/&#123;version&#125;/adapter_model.safetensors
                    </code>{" "}
                    (formato PEFT).
                  </li>
                  <li>
                    O registro no painel valida que o prefixo tem os arquivos
                    antes de gravar na tabela de adapters.
                  </li>
                  <li>
                    Com o adapter ativo, o gateway resolve em qual máquina ele
                    está carregado e reescreve o <code>model</code> internamente
                    para apontar para o adapter da conta.
                  </li>
                </List>
                <p>
                  <span className="font-medium text-foreground">
                    Aviso:
                  </span>{" "}
                  o load/unload dinâmico de LoRA em runtime (sem reiniciar o
                  pod) ainda não foi validado de ponta a ponta contra um pod
                  real com GPU — trate o comportamento como assumido até essa
                  validação acontecer.
                </p>
              </AccordionContent>
            </AccordionItem>

            <AccordionItem value="rag">
              <AccordionTrigger className="text-base font-medium text-foreground">
                System prompt e base de conhecimento (RAG)
              </AccordionTrigger>
              <AccordionContent>
                <Lead>
                  Cada conta pode ter um system prompt próprio e arquivos
                  indexados como base de conhecimento.
                </Lead>
                <List>
                  <li>
                    Em toda chamada de chat, o gateway injeta o system prompt
                    configurado da conta como primeira mensagem.
                  </li>
                  <li>
                    Se a conta tem arquivos indexados, a última mensagem do
                    usuário é usada para buscar os trechos mais similares
                    (embeddings) e injetá-los como contexto antes da mensagem
                    do usuário — é best-effort: se a busca falhar, a chamada
                    segue normalmente, só sem o contexto extra.
                  </li>
                </List>
              </AccordionContent>
            </AccordionItem>

            <AccordionItem value="financeiro">
              <AccordionTrigger className="text-base font-medium text-foreground">
                Financeiro e custos
              </AccordionTrigger>
              <AccordionContent>
                <Lead>
                  A aba Financeiro mostra o histórico de custo por máquina ao
                  longo do tempo, calculado a partir dos eventos que ligam e
                  desligam GPU.
                </Lead>
                <List>
                  <li>
                    Cada troca de status de uma máquina (ligar, pausar,
                    religar automático, provisionar) é um "trigger" que abre
                    ou fecha um intervalo de cobrança.
                  </li>
                  <li>
                    O custo é function do tempo em que a GPU ficou de fato
                    rodando — máquinas pausadas não geram custo de
                    computação.
                  </li>
                </List>
              </AccordionContent>
            </AccordionItem>

            <AccordionItem value="requisicoes">
              <AccordionTrigger className="text-base font-medium text-foreground">
                Requisições e logs
              </AccordionTrigger>
              <AccordionContent>
                <Lead>
                  A página de Requisições lista o histórico de chamadas
                  atendidas pelo gateway, cada uma associada à stack, à chave
                  (prefixo) e à conta que a originou.
                </Lead>
                <List>
                  <li>
                    Serve para auditar uso por conta/chave e diagnosticar
                    problemas de roteamento (ex.: qual máquina atendeu uma
                    chamada específica).
                  </li>
                  <li>
                    O detalhe de cada máquina, na página de Máquinas, também
                    expõe logs (vLLM) e uso por conta na própria máquina.
                  </li>
                </List>
              </AccordionContent>
            </AccordionItem>
          </Accordion>
        </CardContent>
      </Card>
    </div>
  )
}
