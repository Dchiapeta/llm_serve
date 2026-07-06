"use client"

import * as React from "react"
import { ChevronDown } from "lucide-react"

import { cn } from "@/lib/utils"
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible"

// Seção colapsável estilo RunPod (card com cabeçalho e chevron).
export function CollapsibleSection({
  title,
  defaultOpen = false,
  children,
  className,
}: {
  title: string
  defaultOpen?: boolean
  children: React.ReactNode
  className?: string
}) {
  return (
    <Collapsible
      defaultOpen={defaultOpen}
      className={cn("rounded-lg border", className)}
    >
      <CollapsibleTrigger className="group flex w-full items-center justify-between px-4 py-3 text-sm font-medium">
        {title}
        <ChevronDown className="size-4 text-muted-foreground transition-transform group-data-[state=open]:rotate-180" />
      </CollapsibleTrigger>
      <CollapsibleContent className="flex flex-col gap-4 border-t px-4 py-4">
        {children}
      </CollapsibleContent>
    </Collapsible>
  )
}
