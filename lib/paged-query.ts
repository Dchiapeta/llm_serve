// Paginação completa de uma query do PostgREST.
//
// O PostgREST devolve no máximo 1000 linhas por resposta, para QUALQUER
// select. Um `select("*")` sem `range` numa tabela que cresce (usage_metrics,
// machine_runtime_intervals) trunca em silêncio: só as primeiras linhas da
// ordenação chegam e os totais ficam errados sem nenhum erro visível. Usado
// pelo CRM (app/(dashboard)/crm/queries.ts) e pelo Financeiro.

const PAGE = 1000
const MAX_PAGES = 50

export type PagedQuery = {
  range: (
    from: number,
    to: number
  ) => PromiseLike<{ data: unknown[] | null; error: { message: string } | null }>
}

/**
 * A query passada precisa estar ordenada por uma coluna ÚNICA (id). Ordenar por
 * algo repetido — `window_start`, que se repete a cada janela de 2 min — deixa a
 * ordem indefinida entre linhas empatadas, e aí o Postgres pode devolver a mesma
 * linha em duas páginas e pular outra: os totais ficariam errados sem sinal.
 *
 * Erro NÃO é tratado como fim dos dados: sem checar `error`, uma falha devolve
 * data=null, o laço interpreta como "acabou" e a página exibe 0 com o aviso de
 * truncamento desligado — o pior resultado possível, que é mentir calado.
 */
export async function fetchAll<T>(
  build: () => PagedQuery
): Promise<{ rows: T[]; truncated: boolean; failed: boolean }> {
  const rows: T[] = []
  for (let page = 0; page < MAX_PAGES; page++) {
    const from = page * PAGE
    const { data, error } = await build().range(from, from + PAGE - 1)
    if (error) return { rows, truncated: false, failed: true }
    const batch = (data ?? []) as T[]
    rows.push(...batch)
    if (batch.length < PAGE) return { rows, truncated: false, failed: false }
  }
  return { rows, truncated: true, failed: false }
}
