"use client"

import Link from "next/link"
import { usePathname } from "next/navigation"
import {
  Boxes,
  KeyRound,
  LayoutDashboard,
  Server,
} from "lucide-react"

import { cn } from "@/lib/utils"

const items = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard },
  { href: "/machines", label: "Máquinas", icon: Server },
  { href: "/templates", label: "Templates", icon: Boxes },
  { href: "/accounts", label: "Contas & Chaves", icon: KeyRound },
]

export function SidebarNav() {
  const pathname = usePathname()

  return (
    <nav className="flex flex-col gap-1">
      {items.map((item) => {
        const active =
          item.href === "/"
            ? pathname === "/"
            : pathname.startsWith(item.href)
        return (
          <Link
            key={item.href}
            href={item.href}
            className={cn(
              "flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
              active
                ? "bg-primary text-primary-foreground"
                : "text-muted-foreground hover:bg-accent hover:text-accent-foreground"
            )}
          >
            <item.icon className="size-4" />
            {item.label}
          </Link>
        )
      })}
    </nav>
  )
}
