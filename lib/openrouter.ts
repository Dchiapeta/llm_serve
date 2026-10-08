// Catálogo público do OpenRouter (GET /api/v1/models), usado pela página
// /modelos: validar o slug na hora de cadastrar e mostrar preço/contexto na
// tabela. Não precisa de chave. ~1 MB com todas as modalidades, abaixo do teto
// de 2 MB por item do data cache — por isso cabe no revalidate de 1h.

const CATALOG_URL = "https://openrouter.ai/api/v1/models?output_modalities=all"

export type OpenRouterCatalogModel = {
  id: string
  name: string
  context_length: number | null
  architecture?: { input_modalities?: string[]; output_modalities?: string[] }
  // USD por token, como string (formato do OpenRouter)
  pricing?: Record<string, string | undefined>
}

export async function getOpenRouterCatalog(): Promise<Map<string, OpenRouterCatalogModel>> {
  const res = await fetch(CATALOG_URL, {
    next: { revalidate: 3600 },
    signal: AbortSignal.timeout(10_000),
  })
  if (!res.ok) throw new Error(`OpenRouter respondeu ${res.status}`)
  const body = (await res.json()) as { data?: OpenRouterCatalogModel[] }
  return new Map((body.data ?? []).map((m) => [m.id, m]))
}

// Mesmo critério do gateway (openrouter_models.kind): quem GERA imagem atende
// as rotas /v1/images/*; quem gera texto atende chat/messages/responses. O
// resto (decisions, embeddings, rerank, transcription, speech...) não tem rota
// no gateway: null, e o cadastro recusa — senão o cliente leva o erro do
// OpenRouter mandando usar um endpoint que a Stac não expõe.
export function openRouterModelKind(model: OpenRouterCatalogModel): "text" | "image" | null {
  const outputs = model.architecture?.output_modalities ?? []
  if (outputs.includes("image")) return "image"
  if (outputs.includes("text")) return "text"
  return null
}

// "US$ 3,00 / 15,00 por 1M tokens" a partir do preço por token.
export function formatPricePerMillion(model: OpenRouterCatalogModel | undefined): string | null {
  const prompt = Number(model?.pricing?.prompt)
  const completion = Number(model?.pricing?.completion)
  if (!Number.isFinite(prompt) || !Number.isFinite(completion)) return null
  if (prompt === 0 && completion === 0) return "grátis"
  const fmt = (v: number) =>
    (v * 1_000_000).toLocaleString("pt-BR", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })
  return `US$ ${fmt(prompt)} / ${fmt(completion)}`
}
