import { Badge } from "@/components/ui/badge"
import { requestOrigin } from "@/lib/request-origin"

// A regra de classificação vive em lib/request-origin.ts (módulo puro) — ver
// lá o porquê de o rótulo não ser gravado no banco. Aqui só o render.
export function RequestOriginBadge({
  path,
  userAgent,
}: {
  path: string
  userAgent?: string | null
}) {
  const origin = requestOrigin({ path, userAgent })
  return (
    // UA cru no tooltip: quando o rótulo cai no "HTTP" genérico, é o que
    // permite descobrir qual ferramenta nova merece entrar na lista
    <Badge className={origin.className} title={userAgent ?? undefined}>
      {origin.label}
    </Badge>
  )
}
