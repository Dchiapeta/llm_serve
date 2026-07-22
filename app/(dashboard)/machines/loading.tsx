import {
  PageHeaderSkeleton,
  TableCardSkeleton,
} from "@/components/dashboard/skeletons"
import { Skeleton } from "@/components/ui/skeleton"

export default function Loading() {
  return (
    <div className="flex flex-col gap-6">
      <PageHeaderSkeleton action />
      <Skeleton className="h-16 w-full" />
      <TableCardSkeleton />
    </div>
  )
}
