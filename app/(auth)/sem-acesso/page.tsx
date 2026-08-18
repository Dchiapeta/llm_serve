import { ShieldX } from "lucide-react"

import { logout } from "@/lib/actions"
import { allowedDomainsLabel } from "@/lib/auth-admin"
import { createSupabaseServerClient } from "@/lib/supabase/server"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"

export const dynamic = "force-dynamic"

/**
 * Onde cai quem está autenticado mas não pertence à equipe.
 *
 * Acontece de verdade: este painel divide o Supabase Auth com o app do cliente
 * final, então um cliente logado em app.trystac.com chega aqui já autenticado.
 * A tela explica em vez de devolver um 404 confuso, e oferece a saída (sair da
 * conta) para trocar por uma permitida.
 */
export default async function SemAcessoPage() {
  const supabase = await createSupabaseServerClient()
  const { data } = await supabase.auth.getClaims()
  const email = data?.claims?.email as string | undefined

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <div className="mb-2 flex size-10 items-center justify-center rounded-lg bg-muted">
            <ShieldX className="size-5 text-muted-foreground" />
          </div>
          <CardTitle>Acesso restrito</CardTitle>
          <CardDescription>
            Este painel é de uso interno. Apenas contas {allowedDomainsLabel()}{" "}
            podem entrar.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {email && (
            <p className="text-sm text-muted-foreground">
              Você está conectado como{" "}
              <span className="font-medium text-foreground">{email}</span>.
            </p>
          )}
          <form action={logout}>
            <Button type="submit" variant="outline" className="w-full">
              Sair e entrar com outra conta
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}
