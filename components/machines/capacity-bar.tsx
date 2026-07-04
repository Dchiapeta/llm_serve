import { cn } from "@/lib/utils"

// Barra segmentada de alocação de slots, no estilo "Capacity Allocation"
export function CapacityBar({
  used,
  max,
  className,
}: {
  used: number
  max: number
  className?: string
}) {
  const total = Math.max(max, 1)
  const segments = Math.min(total, 60)
  const usedSegments = Math.round((used / total) * segments)

  return (
    <div className={cn("flex h-8 items-end gap-[3px]", className)}>
      {Array.from({ length: segments }).map((_, i) => (
        <div
          key={i}
          className={cn(
            "w-[6px] rounded-full",
            i < usedSegments ? "h-full bg-emerald-500" : "h-full bg-muted"
          )}
        />
      ))}
    </div>
  )
}
