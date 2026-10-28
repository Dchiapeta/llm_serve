import {
  KpiRowSkeleton,
  PageHeaderSkeleton,
  TableCardSkeleton,
} from "@/components/dashboard/skeletons"

export default function Loading() {
  return (
    <div className="flex flex-col gap-6">
      <PageHeaderSkeleton action />
      <KpiRowSkeleton count={4} />
      <TableCardSkeleton />
    </div>
  )
}
