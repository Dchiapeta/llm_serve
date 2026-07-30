import { createHash, randomBytes } from "crypto"

// Gera uma chave com prefixo "stc_" + 64 caracteres HEX (256 bits).
// O prefixo é só cosmético (facilita reconhecer a chave em logs/DevTools);
// a autenticação no gateway faz hash da string inteira, então chaves antigas
// sem prefixo continuam válidas normalmente.
export function generateHexKey(): string {
  return `stc_${randomBytes(32).toString("hex")}`
}

export function hashKey(key: string): string {
  return createHash("sha256").update(key).digest("hex")
}

// Prefixo exibido na UI para identificar a chave sem revelá-la
export function keyPrefix(key: string): string {
  return key.slice(0, 8)
}
