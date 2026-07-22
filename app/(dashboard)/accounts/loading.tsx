import {
  PageHeaderSkeleton,
  TableCardSkeleton,
} from "@/components/dashboard/skeletons"

export default function Loading() {
  return (
    <div className="flex flex-col gap-6">
      <PageHeaderSkeleton action />
      <TableCardSkeleton />
      <TableCardSkeleton rows={4} />
    </div>
  )
}
