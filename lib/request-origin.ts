// De onde veio a requisição: qual ferramenta o cliente apontou pro gateway.
//
// O produto é BYOE (o cliente usa a ferramenta dele contra o nosso endpoint),
// então "qual cliente" é a informação que torna o histórico acionável — dá pra
// separar um Claude Code fazendo turnos de 85k tokens de um script curl de
// teste, mesmo os dois batendo na mesma stack.
//
// Três fontes, nesta ordem:
//   0. purpose da chave (api_keys.purpose, migration 0044) — a chave
//      "playground" é interna e nunca chega ao cliente, então tudo que passa
//      por ela é teste nosso, não uso real. Vem antes do User-Agent de
//      propósito: o UA aqui é o de quem estiver testando (browser do painel,
//      curl), e classificar isso como "HTTP" esconderia justamente a
//      informação que importa — que a linha não é tráfego do cliente.
//   1. User-Agent (gateway_requests.user_agent, migration 0041) — identifica a
//      FERRAMENTA. É o único jeito de separar Cursor/Continue/SDK/curl, que
//      compartilham o mesmo endpoint chat/completions.
//   2. path — identifica o PROTOCOLO, e por construção já entrega dois
//      clientes: o Claude Code só fala /v1/messages e o Codex só fala
//      /v1/responses (ver docker/gateway/anthropic_compat.py). Serve de
//      fallback para requisições anteriores à 0041 e para clientes sem UA.
//
// A classificação mora aqui, e não gravada no banco, de propósito: a lista de
// ferramentas muda mais rápido que o schema, e um rótulo persistido
// congelaria a classificação do dia do insert. Trocar a regra aqui
// reclassifica todo o histórico de uma vez.
//
// O User-Agent é controlado pelo cliente e pode ser forjado à vontade: isto é
// telemetria de conveniência, nunca base para decisão de segurança ou billing.

import type { ApiKeyPurpose } from "@/lib/types"

export type RequestOrigin = {
  label: string
  // Tailwind com o modificador `!` porque os estilos da ReUI
  // (.style-nova .cn-badge-variant-*) têm especificidade maior que utilitários
  // — mesmo motivo de components/machines/plan-badge.tsx.
  className: string
}

const NEUTRO = "bg-zinc-100! text-zinc-600! dark:bg-zinc-900! dark:text-zinc-400!"

const PLAYGROUND: RequestOrigin = {
  label: "Playground",
  className: "bg-amber-100! text-amber-700! dark:bg-amber-950! dark:text-amber-300!",
}

// Nomeadas porque o fallback por protocolo (BY_PATH) também as usa: a lista
// abaixo cresce a cada ferramenta nova, e apontar para a posição no array
// quebraria o fallback no dia em que um padrão entrasse antes destes dois.
const CLAUDE_CODE: RequestOrigin = {
  label: "Claude Code",
  className: "bg-orange-100! text-orange-700! dark:bg-orange-950! dark:text-orange-300!",
}

const CODEX: RequestOrigin = {
  label: "Codex",
  className:
    "bg-emerald-100! text-emerald-700! dark:bg-emerald-950! dark:text-emerald-300!",
}

// Ordem importa: o primeiro match vence. Padrões mais específicos primeiro
// (um cliente pode mandar "cursor" E "openai-node" no mesmo UA).
const BY_USER_AGENT: Array<{ match: string[]; origin: RequestOrigin }> = [
  { match: ["claude-cli", "claude-code", "claudecode"], origin: CLAUDE_CODE },
  { match: ["codex"], origin: CODEX },
  {
    match: ["cursor"],
    origin: {
      label: "Cursor",
      className:
        "bg-indigo-100! text-indigo-700! dark:bg-indigo-950! dark:text-indigo-300!",
    },
  },
  {
    match: ["cline"],
    origin: {
      label: "Cline",
      className: "bg-teal-100! text-teal-700! dark:bg-teal-950! dark:text-teal-300!",
    },
  },
  {
    match: ["roo"],
    origin: {
      label: "Roo",
      className: "bg-teal-100! text-teal-700! dark:bg-teal-950! dark:text-teal-300!",
    },
  },
  {
    match: ["continue"],
    origin: {
      label: "Continue",
      className: "bg-sky-100! text-sky-700! dark:bg-sky-950! dark:text-sky-300!",
    },
  },
  // Automação, não ferramenta de código — e é o perfil de uso que o Go
  // mantém (documents/extract, images/extract, models), então o rótulo
  // próprio é o que separa um workflow de produção de um curl de teste.
  // O n8n manda o UA literal "n8n", sem versão e sem citar o axios que usa
  // por baixo; mesmo assim vem antes de SDK/HTTP, porque no dia em que passar
  // a mandar "n8n (axios/1.7)" o rótulo genérico venceria o específico.
  //
  // Cobre as chamadas do próprio n8n (listagem de modelos, nodes de HTTP e de
  // extração). Os nodes de IA dele falam pelo SDK do LangChain e chegam como
  // "langchainjs-openai/…", indistinguíveis de qualquer app LangChain JS —
  // esses seguem caindo em SDK, e só um UA próprio no node resolveria.
  {
    match: ["n8n"],
    origin: {
      label: "n8n",
      className: "bg-rose-100! text-rose-700! dark:bg-rose-950! dark:text-rose-300!",
    },
  },
  {
    match: ["openai-python", "openai-node", "anthropic-sdk", "anthropic-python", "langchain"],
    origin: {
      label: "SDK",
      className:
        "bg-violet-100! text-violet-700! dark:bg-violet-950! dark:text-violet-300!",
    },
  },
  {
    match: ["curl", "httpie", "python-requests", "postman", "insomnia", "node-fetch", "axios"],
    origin: { label: "HTTP", className: NEUTRO },
  },
]

// Fallback por protocolo, para requisições sem user_agent (anteriores à
// migration 0041, ou cliente que não mandou o header).
const BY_PATH: Record<string, RequestOrigin> = {
  messages: CLAUDE_CODE, // /v1/messages: só o Claude Code fala
  responses: CODEX, // /v1/responses: só o Codex fala
}

export function requestOrigin(input: {
  path: string
  userAgent?: string | null
  // purpose da chave usada na requisição. Opcional: chamadores que não têm o
  // dado (linhas antigas, telas que só conhecem o UA) caem na classificação
  // por ferramenta, como antes.
  keyPurpose?: ApiKeyPurpose | null
}): RequestOrigin {
  if (input.keyPurpose === "playground") return PLAYGROUND

  const ua = input.userAgent?.toLowerCase() ?? ""
  if (ua) {
    for (const { match, origin } of BY_USER_AGENT) {
      if (match.some((needle) => ua.includes(needle))) return origin
    }
    // UA presente mas desconhecido: ainda dá pra aproveitar o protocolo
    // (um cliente novo falando /v1/messages continua sendo Claude Code)
  }
  return BY_PATH[input.path] ?? { label: "HTTP", className: NEUTRO }
}
