// Cliente da API do RunPod.
// REST (rest.runpod.io/v1): pods e templates.
// GraphQL (api.runpod.io/graphql): tipos de GPU (não exposto na REST).

const REST_BASE = "https://rest.runpod.io/v1"
const GRAPHQL_URL = "https://api.runpod.io/graphql"

function apiKey(): string {
  const key = process.env.RUNPOD_API_KEY
  if (!key) throw new Error("RUNPOD_API_KEY não configurada")
  return key
}

async function rest<T>(
  path: string,
  init?: RequestInit & { json?: unknown }
): Promise<T> {
  const { json, ...rest } = init ?? {}
  const res = await fetch(`${REST_BASE}${path}`, {
    ...rest,
    headers: {
      Authorization: `Bearer ${apiKey()}`,
      ...(json !== undefined ? { "Content-Type": "application/json" } : {}),
      ...rest.headers,
    },
    body: json !== undefined ? JSON.stringify(json) : rest.body,
    cache: "no-store",
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`RunPod ${rest.method ?? "GET"} ${path} → ${res.status}: ${text}`)
  }
  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

// ---------- Pods ----------

export type RunPodPod = {
  id: string
  name: string
  desiredStatus: string
  image: string
  costPerHr: number
  gpu?: { id?: string; displayName?: string; count?: number } & Record<string, unknown>
  machine?: Record<string, unknown>
  env?: Record<string, string>
  portMappings?: Record<string, number> | null
  publicIp?: string
  lastStartedAt?: string
  memoryInGb?: number
  vcpuCount?: number
  containerDiskInGb?: number
}

export type CreatePodInput = {
  name: string
  imageName: string
  gpuTypeIds: string[]
  gpuCount?: number
  containerDiskInGb?: number
  env?: Record<string, string>
  ports?: string[]
  cloudType?: "SECURE" | "COMMUNITY"
  templateId?: string
  dockerStartCmd?: string[]
}

export type RunPodTemplate = {
  id: string
  name: string
  imageName: string
  containerDiskInGb?: number
  volumeInGb?: number
  volumeMountPath?: string
  category?: string
  ports?: string[]
  env?: Record<string, string>
  dockerStartCmd?: string[]
  readme?: string
}

export const runpod = {
  listPods: () => rest<RunPodPod[]>("/pods"),
  getPod: (podId: string) => rest<RunPodPod>(`/pods/${podId}`),
  createPod: (input: CreatePodInput) =>
    rest<RunPodPod>("/pods", { method: "POST", json: input }),
  stopPod: (podId: string) =>
    rest<RunPodPod>(`/pods/${podId}/stop`, { method: "POST" }),
  startPod: (podId: string) =>
    rest<RunPodPod>(`/pods/${podId}/start`, { method: "POST" }),
  deletePod: (podId: string) =>
    rest<void>(`/pods/${podId}`, { method: "DELETE" }),

  // ---------- Templates ----------
  listTemplates: () => rest<RunPodTemplate[]>("/templates"),
  createTemplate: (input: {
    name: string
    imageName: string
    containerDiskInGb?: number
    env?: Record<string, string>
    ports?: string[]
    dockerStartCmd?: string[]
    isServerless?: boolean
  }) => rest<{ id: string }>("/templates", { method: "POST", json: input }),
  updateTemplate: (
    templateId: string,
    input: {
      name?: string
      imageName?: string
      containerDiskInGb?: number
      env?: Record<string, string>
      ports?: string[]
      dockerStartCmd?: string[]
    }
  ) => rest<{ id: string }>(`/templates/${templateId}`, { method: "PATCH", json: input }),
  deleteTemplate: (templateId: string) =>
    rest<void>(`/templates/${templateId}`, { method: "DELETE" }),
}

// ---------- GPU types (GraphQL) ----------

export type GpuType = {
  id: string
  displayName: string
  memoryInGb: number
  securePrice: number | null
  communityPrice: number | null
}

export async function listGpuTypes(): Promise<GpuType[]> {
  const res = await fetch(GRAPHQL_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${apiKey()}`,
    },
    body: JSON.stringify({
      query: `query { gpuTypes { id displayName memoryInGb securePrice communityPrice } }`,
    }),
    cache: "no-store",
  })
  if (!res.ok) throw new Error(`RunPod GraphQL → ${res.status}`)
  const data = await res.json()
  if (data.errors?.length) throw new Error(data.errors[0].message)
  return data.data.gpuTypes
}

// URL pública do proxy do RunPod para uma porta do pod
export function podProxyUrl(podId: string, port: number): string {
  return `https://${podId}-${port}.proxy.runpod.net`
}
