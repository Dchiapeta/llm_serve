import { createHash, randomBytes } from "crypto"

// Gera uma chave HEX de 64 caracteres (256 bits)
export function generateHexKey(): string {
  return randomBytes(32).toString("hex")
}

export function hashKey(key: string): string {
  return createHash("sha256").update(key).digest("hex")
}

// Prefixo exibido na UI para identificar a chave sem revelá-la
export function keyPrefix(key: string): string {
  return key.slice(0, 8)
}
