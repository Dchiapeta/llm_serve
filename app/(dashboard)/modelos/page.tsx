import { Suspense } from "react"
import Link from "next/link"

import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { CreateOpenRouterModelDialog } from "@/components/openrouter/create-openrouter-model-dialog"

import { ModelosBody } from "./modelos-body"

export const dynamic = "force-dynamic"

export default function ModelosPage() {
  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Modelos (OpenRouter)</h1>
          <p className="text-sm text-muted-foreground">
            Modelos atendidos pelo OpenRouter em vez das máquinas da Stac ·{" "}
            <Link href="/documentacao/openrouter" className="underline underline-offset-4">
              como funciona (temporário)
            </Link>
          </p>
        </div>
        <CreateOpenRouterModelDialog />
      </div>

      <Suspense fallback={<ModelosBodySkeleton />}>
        <ModelosBody />
      </Suspense>
    </div>
  )
}

function ModelosBodySkeleton() {
  return (
    <div className="flex flex-col gap-6">
      <Skeleton className="h-16 w-full" />
      <Card>
        <CardHeader className="flex flex-col gap-2">
          <Skeleton className="h-5 w-48" />
          <Skeleton className="h-3 w-40" />
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </CardContent>
      </Card>
    </div>
  )
}
