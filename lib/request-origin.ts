// De onde veio a requisição: qual ferramenta o cliente apontou pro gateway.
//
// O produto é BYOE (o cliente usa a ferramenta dele contra o nosso endpoint),
// então "qual cliente" é a informação que torna o histórico acionável — dá pra
// separar um Claude Code fazendo turnos de 85k tokens de um script curl de
// teste, mesmo os dois batendo na mesma stack.
//
// Duas fontes, nesta ordem:
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

export type RequestOrigin = {
  label: string
  // Tailwind com o modificador `!` porque os estilos da ReUI
  // (.style-nova .cn-badge-variant-*) têm especificidade maior que utilitários
  // — mesmo motivo de components/machines/plan-badge.tsx.
  className: string
}

const NEUTRO = "bg-zinc-100! text-zinc-600! dark:bg-zinc-900! dark:text-zinc-400!"

// Ordem importa: o primeiro match vence. Padrões mais específicos primeiro
// (um cliente pode mandar "cursor" E "openai-node" no mesmo UA).
const BY_USER_AGENT: Array<{ match: string[]; origin: RequestOrigin }> = [
  {
    match: ["claude-cli", "claude-code", "claudecode"],
    origin: {
      label: "Claude Code",
      className:
        "bg-orange-100! text-orange-700! dark:bg-orange-950! dark:text-orange-300!",
    },
  },
  {
    match: ["codex"],
    origin: {
      label: "Codex",
      className:
        "bg-emerald-100! text-emerald-700! dark:bg-emerald-950! dark:text-emerald-300!",
    },
  },
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
  messages: BY_USER_AGENT[0].origin, // /v1/messages: só o Claude Code fala
  responses: BY_USER_AGENT[1].origin, // /v1/responses: só o Codex fala
}

export function requestOrigin(input: {
  path: string
  userAgent?: string | null
}): RequestOrigin {
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
