"use client"

import * as React from "react"
import { icons } from "lucide-react"

type IconPlaceholderProps = {
  lucide?: string
  tabler?: string
  hugeicons?: string
  phosphor?: string
  remixicon?: string
} & React.ComponentProps<"svg">

// Shim do IconPlaceholder do ReUI: aqui usamos sempre lucide.
export function IconPlaceholder({
  lucide,
  tabler: _tabler,
  hugeicons: _hugeicons,
  phosphor: _phosphor,
  remixicon: _remixicon,
  ...props
}: IconPlaceholderProps) {
  const name = lucide?.replace(/Icon$/, "")
  const Icon = name ? icons[name as keyof typeof icons] : null
  if (!Icon) return null
  return <Icon {...props} />
}
