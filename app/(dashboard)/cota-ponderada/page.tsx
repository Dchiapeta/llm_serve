import { redirect } from "next/navigation"

// A análise da request ponderada virou uma seção da página Uso.
export default function CotaPonderadaPage() {
  redirect("/uso")
}
