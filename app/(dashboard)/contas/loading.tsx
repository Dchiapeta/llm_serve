import {
  PageHeaderSkeleton,
  TableCardSkeleton,
} from "@/components/dashboard/skeletons"

export default function Loading() {
  return (
    <div className="flex flex-col gap-6">
      <PageHeaderSkeleton />
      <TableCardSkeleton />
    </div>
  )
}
