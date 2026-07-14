// Gerador do ID legível de uma stack ("brisk-falcon-42"), usado como
// subdomínio de acesso do cliente ao manager. Não é segredo — Math.random
// basta (material criptográfico fica em lib/keys.ts). A unicidade é
// garantida pelo unique index em stacks.slug + retry no insert (lib/actions.ts).
// Isomórfico de propósito: o dialog gera o preview no client.

const ADJECTIVES = [
  "brisk", "calm", "bold", "swift", "quiet", "vivid", "witty", "sunny",
  "lucid", "merry", "noble", "rapid", "solid", "tidy", "zesty", "agile",
  "amber", "azure", "cosmic", "crisp", "dandy", "eager", "fancy", "gentle",
  "happy", "ivory", "jolly", "keen", "lively", "mellow", "nimble", "polar",
  "proud", "royal", "sharp", "silent", "steady", "stellar", "urban", "wild",
] as const

const NOUNS = [
  "falcon", "otter", "maple", "comet", "harbor", "lynx", "ember", "cedar",
  "delta", "onyx", "ridge", "sable", "tundra", "vertex", "willow", "zephyr",
  "aspen", "badger", "beacon", "canyon", "coral", "crane", "dune", "fjord",
  "gecko", "glade", "heron", "iris", "jaguar", "lagoon", "meteor", "nebula",
  "orbit", "osprey", "pine", "quartz", "raven", "reef", "summit", "wolf",
] as const

const pick = <T,>(arr: readonly T[]) => arr[Math.floor(Math.random() * arr.length)]

export function generateStackSlug(): string {
  return `${pick(ADJECTIVES)}-${pick(NOUNS)}-${10 + Math.floor(Math.random() * 90)}`
}

export const STACK_SLUG_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/
