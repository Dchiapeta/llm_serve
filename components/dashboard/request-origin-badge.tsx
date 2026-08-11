import { Badge } from "@/components/ui/badge"
import { requestOrigin } from "@/lib/request-origin"
import type { ApiKeyPurpose } from "@/lib/types"

// A regra de classificação vive em lib/request-origin.ts (módulo puro) — ver
// lá o porquê de o rótulo não ser gravado no banco. Aqui só o render.
export function RequestOriginBadge({
  path,
  userAgent,
  keyPurpose,
}: {
  path: string
  userAgent?: string | null
  keyPurpose?: ApiKeyPurpose | null
}) {
  const origin = requestOrigin({ path, userAgent, keyPurpose })
  return (
    // UA cru no tooltip: quando o rótulo cai no "HTTP" genérico, é o que
    // permite descobrir qual ferramenta nova merece entrar na lista
    <Badge className={origin.className} title={userAgent ?? undefined}>
      {origin.label}
    </Badge>
  )
}
