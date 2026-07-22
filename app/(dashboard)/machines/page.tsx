import { Suspense } from "react"

import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"

import { MachinesBody } from "./machines-body"
import { MachinesToolbar } from "./machines-toolbar"

export const dynamic = "force-dynamic"

export default function MachinesPage() {
  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Máquinas</h1>
          <p className="text-sm text-muted-foreground">
            Pods rodando LLMs no RunPod
          </p>
        </div>
        <Suspense fallback={<Skeleton className="h-9 w-36" />}>
          <MachinesToolbar />
        </Suspense>
      </div>

      <Suspense fallback={<MachinesBodySkeleton />}>
        <MachinesBody />
      </Suspense>
    </div>
  )
}

function MachinesBodySkeleton() {
  return (
    <div className="flex flex-col gap-6">
      <Skeleton className="h-16 w-full" />
      <Card>
        <CardHeader className="flex flex-col gap-2">
          <Skeleton className="h-5 w-48" />
          <Skeleton className="h-3 w-40" />
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </CardContent>
      </Card>
    </div>
  )
}
