import { cookies } from "next/headers"

import { requireAdminSession } from "@/lib/auth-admin-server"
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
  // Autoriza E devolve o e-mail para exibir — a mesma leitura de claims que já
  // acontecia aqui. O proxy.ts faz o corte otimista, mas esta é a camada que de
  // fato garante: cobre todas as rotas do grupo mesmo se o matcher do proxy
  // mudar, como a doc do Next 16 recomenda.
  const email = await requireAdminSession()

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
