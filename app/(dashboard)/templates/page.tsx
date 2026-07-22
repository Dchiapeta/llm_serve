import { Suspense } from "react"

import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"

import { TemplatesBody } from "./templates-body"
import { TemplatesToolbar } from "./templates-toolbar"

export const dynamic = "force-dynamic"

export default function TemplatesPage() {
  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Produtos</h1>
          <p className="text-sm text-muted-foreground">
            Configurações de imagem e modelo para criar máquinas
          </p>
        </div>
        <Suspense fallback={<Skeleton className="h-9 w-36" />}>
          <TemplatesToolbar />
        </Suspense>
      </div>

      <Suspense
        fallback={
          <Card>
            <CardHeader className="flex flex-col gap-2">
              <Skeleton className="h-5 w-40" />
              <Skeleton className="h-3 w-56" />
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              {Array.from({ length: 6 }).map((_, i) => (
                <Skeleton key={i} className="h-10 w-full" />
              ))}
            </CardContent>
          </Card>
        }
      >
        <TemplatesBody />
      </Suspense>
    </div>
  )
}
