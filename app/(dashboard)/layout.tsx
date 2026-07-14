import { Cpu } from "lucide-react"

import { createSupabaseServerClient } from "@/lib/supabase/server"
import { Separator } from "@/components/ui/separator"
import { SidebarNav } from "@/components/dashboard/sidebar-nav"
import { SidebarUserMenu } from "@/components/dashboard/sidebar-user-menu"

export default async function DashboardLayout({
  children,
}: {
  children: React.ReactNode
}) {
  const supabase = await createSupabaseServerClient()
  const {
    data: { user },
  } = await supabase.auth.getUser()

  return (
    <div className="flex min-h-screen">
      <aside className="fixed inset-y-0 left-0 z-30 flex w-60 flex-col border-r bg-background p-4">
        <div className="flex items-center gap-2 px-2 py-3">
          <div className="flex size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <Cpu className="size-4" />
          </div>
          <div className="leading-tight">
            <p className="text-sm font-semibold">LLM Manager</p>
            <p className="text-xs text-muted-foreground">RunPod</p>
          </div>
        </div>
        <Separator className="my-3" />
        <SidebarNav />
        <div className="mt-auto">
          <Separator className="my-3" />
          <SidebarUserMenu email={user?.email ?? "Conta"} />
        </div>
      </aside>
      <main className="ml-60 flex-1 p-6 lg:p-8">{children}</main>
    </div>
  )
}
