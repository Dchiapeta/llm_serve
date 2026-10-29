import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { Toaster } from "@/components/ui/sonner";
import { ThemeProvider } from "@/components/theme-provider";
import { TooltipProvider } from "@/components/ui/tooltip";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "RunPod LLM Manager",
  description: "Gerenciador de infraestrutura de LLM no RunPod",
  icons: {
    // Mesma marca e mesmo esquema do TryStac. O .ico (marca escura) é o
    // app/favicon.ico, que o Next já declara sozinho — repetir aqui duplicava a
    // tag. O SVG troca de cor com o tema pela media query interna: o Chrome
    // ignora `media` no <link rel="icon">, então não dá para trocar por arquivo.
    icon: [{ url: "/brand/favicon.svg", sizes: "any", type: "image/svg+xml" }],
    apple: { url: "/brand/apple-touch-icon.png", sizes: "180x180" },
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="pt-BR"
      className={`${geistSans.variable} ${geistMono.variable} style-nova h-full antialiased`}
      suppressHydrationWarning
    >
      <body
        className="min-h-full flex flex-col bg-muted/40"
        suppressHydrationWarning
      >
        <ThemeProvider attribute="class" defaultTheme="system" enableSystem>
          <TooltipProvider>
            {children}
            <Toaster position="top-right" />
          </TooltipProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
