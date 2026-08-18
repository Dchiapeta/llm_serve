import { Suspense } from "react"

import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"

import { CrmBody } from "./crm-body"

export const dynamic = "force-dynamic"

export type CrmSearchParams = {
  period?: string
  q?: string
  plano?: string
  cobranca?: string
  escopo?: string
}

export default function CrmPage({
  searchParams,
}: {
  searchParams: Promise<CrmSearchParams>
}) {
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold">CRM</h1>
        <p className="text-sm text-muted-foreground">
          Clientes, assinaturas e consumo — dados de cobrança direto da Chargefy
        </p>
      </div>

      {/* searchParams resolvido no body (não aqui): é o que mantém o título
          instantâneo enquanto as queries e a Chargefy streamam */}
      <Suspense fallback={<CrmBodySkeleton />}>
        <CrmBody searchParamsPromise={searchParams} />
      </Suspense>
    </div>
  )
}

function CrmBodySkeleton() {
  return (
    <>
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 8 }).map((_, i) => (
          <Card key={i}>
            <CardHeader className="flex flex-col gap-2 pb-2">
              <Skeleton className="h-4 w-28" />
            </CardHeader>
            <CardContent>
              <Skeleton className="h-8 w-24" />
              <Skeleton className="mt-2 h-3 w-32" />
            </CardContent>
          </Card>
        ))}
      </div>
      <Card>
        <CardHeader className="flex flex-col gap-2">
          <Skeleton className="h-5 w-40" />
          <Skeleton className="h-3 w-64" />
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {Array.from({ length: 8 }).map((_, i) => (
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </CardContent>
      </Card>
    </>
  )
}
