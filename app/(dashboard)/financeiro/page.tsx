import { Suspense } from "react"

import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"

import { FinanceiroBody } from "./financeiro-body"

export const dynamic = "force-dynamic"

export default function FinanceiroPage({
  searchParams,
}: {
  searchParams: Promise<{ period?: string; bucket?: string }>
}) {
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold">Financeiro</h1>
        <p className="text-sm text-muted-foreground">
          Gasto com máquinas ligadas e economia do liga/desliga automático
        </p>
      </div>

      {/* searchParams resolvido no body (não aqui): é o que mantém o título
          instantâneo enquanto as queries streamam */}
      <Suspense fallback={<FinanceiroBodySkeleton />}>
        <FinanceiroBody searchParamsPromise={searchParams} />
      </Suspense>
    </div>
  )
}

function FinanceiroBodySkeleton() {
  return (
    <>
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        {Array.from({ length: 4 }).map((_, i) => (
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
          <Skeleton className="h-5 w-48" />
          <Skeleton className="h-3 w-64" />
        </CardHeader>
        <CardContent>
          <Skeleton className="h-64 w-full" />
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="flex flex-col gap-2">
          <Skeleton className="h-5 w-40" />
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </CardContent>
      </Card>
    </>
  )
}
