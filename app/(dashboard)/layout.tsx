import { cookies } from "next/headers"

import { createSupabaseServerClient } from "@/lib/supabase/server"
import { AppSidebar } from "@/components/dashboard/app-sidebar"
import { ThemeToggle } from "@/components/dashboard/theme-toggle"
import {
  SidebarInset,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar"

export default async function DashboardLayout({
  children,
}: {
  children: React.ReactNode
}) {
  const supabase = await createSupabaseServerClient()
  // Só precisamos do email para exibir; getClaims lê do JWT (sem round-trip
  // quando há signing keys assimétricas). A proteção da rota já é feita no
  // middleware (proxy.ts).
  const { data: claims } = await supabase.auth.getClaims()
  const email = (claims?.claims?.email as string | undefined) ?? "Conta"

  const cookieStore = await cookies()
  const defaultOpen = cookieStore.get("sidebar_state")?.value !== "false"

  return (
    <SidebarProvider defaultOpen={defaultOpen}>
      <AppSidebar email={email} />
      <SidebarInset>
        <header className="flex h-14 shrink-0 items-center gap-2 border-b px-4">
          <SidebarTrigger className="-ml-1" />
          <ThemeToggle className="ml-auto" />
        </header>
        <div className="flex-1 p-6 lg:p-8">{children}</div>
      </SidebarInset>
    </SidebarProvider>
  )
}
