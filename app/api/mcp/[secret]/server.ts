// Ferramentas do conector MCP: os dados do painel, só leitura.
//
// Cada ferramenta reusa a mesma camada de dados da página correspondente
// (loadCrmData, loadCostData), para o que o Claude responde bater com o que o
// painel mostra. Nada aqui grava, provisiona ou chama o RunPod — por isso a
// lista de máquinas lê o status do banco em vez de rodar o reconcile.
//
// Campos são escolhidos um a um: `machines.admin_secret` e afins nunca podem
// sair daqui, então nada de repassar a linha inteira do banco.

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js"
import { z } from "zod"

import { summarizeCost } from "@/lib/billing"
import { applyCrmFilters, summarizeCrm, type CrmRow } from "@/lib/crm"
import { createSupabaseAdmin } from "@/lib/supabase/server"
import { TEMPLATE_PLANS, type Machine } from "@/lib/types"

import {
  CRM_PERIOD_LABELS,
  CRM_PERIOD_MS,
  loadCrmData,
  type CrmPeriod,
} from "../../../(dashboard)/crm/queries"
import { loadCostData } from "../../../(dashboard)/financeiro/queries"

const PERIODS = ["24h", "7d", "30d", "total"] as const satisfies readonly CrmPeriod[]

const periodo = z
  .enum(PERIODS)
  .default("30d")
  .describe("Janela de uso e custo: 24h, 7d, 30d ou total (todo o histórico).")

const BILLING = ["active", "trialing", "past_due", "suspended", "canceled"] as const

const READ_ONLY = { readOnlyHint: true, openWorldHint: false } as const

/** Centavos → reais. O CRM guarda dinheiro em centavos. */
const reais = (cents: number) => Math.round(cents) / 100

const usd = (value: number) => Math.round(value * 100) / 100

function json(data: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(data, null, 2) }] }
}

function clientSummary(r: CrmRow) {
  return {
    conta_id: r.accountId,
    nome: r.name,
    email: r.email,
    tipo: r.kind,
    planos: r.plans,
    stacks: r.stacks.length,
    cobranca: r.billingStatus,
    inadimplente_desde: r.pastDueSince,
    assinatura_chargefy: r.subscriptionStatuses,
    divergencia_banco_chargefy: r.subscriptionDivergence,
    mensal_tabela: reais(r.monthlyGrossCents),
    mensal_cobrado: reais(r.monthlyNetCents),
    moeda: r.currency,
    cupom: r.hasDiscount,
    proxima_cobranca: r.nextBillingAt,
    cancela_no_fim_do_periodo: r.cancelAtPeriodEnd,
    cliente_desde: r.customerSince,
    ultimo_uso: r.lastUsedAt,
    tokens: r.tokens,
    imagens: r.images,
    requisicoes: r.requests,
    chaves_ativas: r.activeKeys,
    custo_gpu_usd: usd(r.gpuCostUsd),
    margem_brl: r.marginBrlCents === null ? null : reais(r.marginBrlCents),
  }
}

/** Avisos que a página mostra em banner — sem eles o Claude trataria número parcial como total. */
function dataWarnings(data: Awaited<ReturnType<typeof loadCrmData>>): string[] {
  const warnings: string[] = []
  if (!data.chargefy.ok) {
    warnings.push(
      `Chargefy indisponível (${data.chargefy.error ?? data.chargefy.reason}): receita, MRR e margem estão incompletos.`
    )
  }
  if (data.chargefy.truncated) warnings.push("Lista de assinaturas da Chargefy veio truncada.")
  if (data.coreIncomplete) warnings.push("Alguma tabela base (contas, stacks, chaves ou máquinas) falhou ao carregar.")
  if (data.usageTruncated) warnings.push("Uso (tokens/requisições) veio incompleto.")
  if (data.costTruncated) warnings.push("Custo de GPU veio incompleto.")
  return warnings
}

/** `origin` = origem pública do manager, para as URLs dos ícones (public/brand). */
export function buildMcpServer(origin: string): McpServer {
  // `theme` diz o fundo para o qual o ícone foi desenhado: marca escura no
  // tema claro, marca clara no escuro.
  const server = new McpServer({
    name: "stac-manager",
    title: "Stac Manager",
    version: "1.0.0",
    icons: [
      { src: `${origin}/brand/icon-light.png`, mimeType: "image/png", sizes: ["512x512"], theme: "light" },
      { src: `${origin}/brand/icon-dark.png`, mimeType: "image/png", sizes: ["512x512"], theme: "dark" },
    ],
  })

  server.registerTool(
    "resumo_negocio",
    {
      title: "Resumo do negócio",
      description:
        "KPIs do CRM: MRR, clientes pagantes, ativos e em risco, ticket médio, consumo, custo de GPU e margem. Valores em reais (BRL), exceto os campos *_usd.",
      inputSchema: { periodo },
      annotations: READ_ONLY,
    },
    async ({ periodo }) => {
      const data = await loadCrmData(periodo)
      const k = summarizeCrm(applyCrmFilters(data.rows, { escopo: "clientes" }))
      return json({
        periodo: CRM_PERIOD_LABELS[periodo],
        mrr: reais(k.mrrCents),
        mrr_tabela: reais(k.mrrGrossCents),
        clientes_total: k.totalCustomers,
        clientes_com_assinatura: k.withSubscription,
        clientes_pagantes: k.payingCustomers,
        // Janela fixa de 7 dias (summarizeCrm), independente de `periodo`.
        clientes_ativos_7d: k.activeCustomers,
        clientes_em_risco: k.atRiskCustomers,
        mrr_em_risco: reais(k.atRiskCents),
        ticket_medio: reais(k.avgTicketCents),
        tokens: k.tokens,
        imagens: k.images,
        requisicoes: k.requests,
        custo_gpu_rateado_usd: usd(k.gpuCostUsd),
        // Inclui máquina sem stack (teste, pod órfão) — por isso é maior que o rateado.
        custo_gpu_total_usd: usd(data.gpuCostTotalUsd),
        margem: reais(k.marginBrlCents),
        margem_completa: k.marginKnown,
        assinaturas_sem_conta: data.unmatchedSubscriptions.map((s) => ({
          id: s.id,
          status: s.status,
          mensal: reais(s.monthlyCents),
        })),
        tokens_sem_conta: data.unattributedTokens,
        avisos: dataWarnings(data),
      })
    }
  )

  server.registerTool(
    "listar_clientes",
    {
      title: "Listar clientes",
      description:
        "Contas do CRM, ordenadas por receita mensal (maior primeiro). Mesmos filtros da tela de CRM. Valores em reais (BRL), exceto custo_gpu_usd.",
      inputSchema: {
        periodo,
        busca: z
          .string()
          .optional()
          .describe("Trecho do nome, e-mail, id da conta ou slug de stack."),
        plano: z.enum(TEMPLATE_PLANS as [string, ...string[]]).optional(),
        cobranca: z.enum(BILLING).optional().describe("Estado de cobrança no banco."),
        escopo: z
          .enum(["clientes", "todos", "internos"])
          .default("clientes")
          .describe("clientes = só quem contratou; internos = equipe; todos = inclui leads sem contratação."),
        limite: z.number().int().min(1).max(500).default(50),
      },
      annotations: READ_ONLY,
    },
    async ({ periodo, busca, plano, cobranca, escopo, limite }) => {
      const data = await loadCrmData(periodo)
      const rows = applyCrmFilters(data.rows, { q: busca, plano, cobranca, escopo })
      return json({
        periodo: CRM_PERIOD_LABELS[periodo],
        total: rows.length,
        exibidos: Math.min(rows.length, limite),
        clientes: rows.slice(0, limite).map(clientSummary),
        avisos: dataWarnings(data),
      })
    }
  )

  server.registerTool(
    "detalhe_cliente",
    {
      title: "Detalhe do cliente",
      description:
        "Uma conta com todas as stacks: plano, máquina, cobrança, assinatura, consumo e custo de GPU de cada uma. Use listar_clientes para achar o conta_id.",
      inputSchema: {
        conta_id: z.string().describe("accountId vindo de listar_clientes."),
        periodo,
      },
      annotations: READ_ONLY,
    },
    async ({ conta_id, periodo }) => {
      const data = await loadCrmData(periodo)
      const row = data.rows.find((r) => r.accountId === conta_id)
      if (!row) {
        return { ...json({ erro: `Conta ${conta_id} não encontrada.` }), isError: true }
      }
      return json({
        periodo: CRM_PERIOD_LABELS[periodo],
        ...clientSummary(row),
        ambientes: row.envs,
        limite_ambientes: row.envLimit,
        chargefy_customer_id: row.chargefyCustomerId,
        vinculo_chargefy: row.matchSource,
        assinaturas_orfas: row.orphanSubscriptions,
        stacks: row.stacks.map((s) => ({
          stack_id: s.id,
          slug: s.slug,
          nome: s.name,
          plano: s.plan,
          categoria: s.category,
          maquina: s.machineName,
          maquina_id: s.machineId,
          cobranca: s.billingStatusDb,
          inadimplente_desde: s.pastDueSince,
          classe_de_uso: s.usageClass,
          data_compra: s.purchaseDate,
          ultima_atividade: s.lastActivityAt,
          tokens: s.tokens,
          imagens: s.images,
          requisicoes: s.requests,
          chaves_ativas: s.activeKeys,
          custo_gpu_usd: usd(s.gpuCostUsd),
          assinatura_id: s.subscriptionId,
          assinatura_status: s.subscriptionStatus,
        })),
        avisos: dataWarnings(data),
      })
    }
  )

  server.registerTool(
    "financeiro",
    {
      title: "Gasto com máquinas",
      description:
        "Gasto de GPU no período (USD), quanto custaria 24/7, a economia do liga/desliga automático e o detalhe por máquina. Mesmos números da página Financeiro.",
      inputSchema: { periodo },
      annotations: READ_ONLY,
    },
    async ({ periodo }) => {
      const { machines, intervals, dataIncomplete, from, to } = await loadCostData(
        CRM_PERIOD_MS[periodo]
      )
      const s = summarizeCost(intervals, machines, from, to)
      const running = machines.filter((m) => m.status === "running")
      return json({
        periodo: CRM_PERIOD_LABELS[periodo],
        de: new Date(from).toISOString(),
        ate: new Date(to).toISOString(),
        gasto_usd: usd(s.spent),
        custo_se_24_7_usd: usd(s.baseline),
        economia_usd: usd(s.saved),
        economia_pct: Math.round(s.savedPct * 10) / 10,
        horas_ligadas: Math.round(s.billableHours * 10) / 10,
        horas_existentes: Math.round(s.lifetimeHours * 10) / 10,
        maquinas_sem_preco: s.unpricedMachines,
        custo_por_hora_agora_usd: usd(running.reduce((t, m) => t + (m.cost_per_hr ?? 0), 0)),
        maquinas_ligadas_agora: running.length,
        por_maquina: s.byMachine.map((r) => ({
          maquina: r.machine?.name ?? r.machineId,
          gpu: r.machine?.gpu_type ?? null,
          status_atual: r.machine?.status ?? null,
          custo_hora_usd: r.costPerHr,
          horas_ligada: Math.round(r.billableHours * 10) / 10,
          gasto_usd: usd(r.spent),
          economia_usd: usd(r.saved),
          uptime_pct: Math.round(r.uptimePct * 10) / 10,
        })),
        avisos: dataIncomplete ? ["Máquinas ou intervalos vieram incompletos."] : [],
      })
    }
  )

  server.registerTool(
    "maquinas",
    {
      title: "Máquinas",
      description:
        "Máquinas RunPod não terminadas: status, GPU, modelo servido, custo por hora, stacks hospedadas e última atividade. O status é o do banco (não consulta o RunPod).",
      inputSchema: {},
      annotations: READ_ONLY,
    },
    async () => {
      const db = createSupabaseAdmin()
      const [machinesRes, stacksRes] = await Promise.all([
        db
          .from("machines")
          .select(
            "id, name, status, gpu_type, model_name, served_model_name, vram_gb, cost_per_hr, max_users, last_activity_at, created_at"
          )
          .neq("status", "terminated")
          .order("created_at", { ascending: false }),
        db.from("stacks").select("machine_id").not("machine_id", "is", null),
      ])
      if (machinesRes.error) {
        return { ...json({ erro: machinesRes.error.message }), isError: true }
      }

      const stacksByMachine = new Map<string, number>()
      for (const s of stacksRes.data ?? []) {
        stacksByMachine.set(s.machine_id, (stacksByMachine.get(s.machine_id) ?? 0) + 1)
      }

      type MachineRow = Pick<
        Machine,
        | "id"
        | "name"
        | "status"
        | "gpu_type"
        | "model_name"
        | "served_model_name"
        | "vram_gb"
        | "cost_per_hr"
        | "max_users"
        | "last_activity_at"
        | "created_at"
      >
      const machines = (machinesRes.data ?? []) as MachineRow[]
      return json({
        total: machines.length,
        maquinas: machines.map((m) => ({
          maquina_id: m.id,
          nome: m.name,
          status: m.status,
          gpu: m.gpu_type,
          vram_gb: m.vram_gb,
          modelo: m.served_model_name ?? m.model_name,
          custo_hora_usd: m.cost_per_hr,
          stacks_hospedadas: stacksByMachine.get(m.id) ?? 0,
          max_usuarios: m.max_users,
          ultima_atividade: m.last_activity_at,
          criada_em: m.created_at,
        })),
        avisos: stacksRes.error ? ["Contagem de stacks por máquina falhou."] : [],
      })
    }
  )

  return server
}
