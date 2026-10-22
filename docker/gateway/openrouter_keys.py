"""Uma chave do OpenRouter para cada chave da Stac.

Decisão de 29/09/2026: o custo do repasse ao OpenRouter precisa ser visível
POR CHAVE no próprio OpenRouter (página Activity, que agrupa por API key). Para
isso cada chave da Stac ganha uma chave espelho lá, criada pela API de
gerenciamento (POST /api/v1/keys, exige uma Management API key), sem teto de
gasto por enquanto — só acompanhamento.

Quando a chave espelho nasce:
  * na criação da chave da Stac: o painel (createKey, que é por onde passam
    tanto o painel interno quanto o TryStac) pede ao gateway em background;
  * senão, na primeira request dela que for para o OpenRouter — cobre as
    chaves anteriores a isto e qualquer criação que tenha falhado.

O segredo da chave espelho só é devolvido UMA vez, na criação, e o gateway
precisa dele em texto puro para chamar o OpenRouter — hash não serve. Por isso
vai cifrado (Fernet) para a tabela openrouter_keys (migration 0072), que é só
service role. Não é uma coluna de api_keys de propósito: o cliente do TryStac
lê a própria api_keys via RLS.

Tudo aqui é best-effort: nenhuma falha deste mecanismo derruba uma request —
sem a chave espelho, o repasse usa a chave compartilhada (OPENROUTER_API_KEY)
e o custo continua em gateway_requests.cost_usd.

Funções PURAS (sem env/rede/FastAPI), mesma disciplina de openrouter.py.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

MAX_NAME_CHARS = 120


def key_name(entry: dict) -> str:
    """Nome da chave espelho no OpenRouter — é o que aparece na Activity.

    `<id da conta>/<id da chave>`, os dois completos (decisão do usuário,
    29/09/2026): liga a linha do OpenRouter direto a accounts e api_keys, sem
    ambiguidade entre chaves com o mesmo prefixo."""
    account_id = str(entry.get("account_id") or "sem-conta")
    api_key_id = str(entry.get("api_key_id") or "")
    return f"{account_id}/{api_key_id}"[:MAX_NAME_CHARS]


def parse_created(payload) -> tuple[str, str]:
    """(hash, segredo) da resposta do POST /api/v1/keys. O segredo vem no
    topo (`key`), o identificador em `data.hash`."""
    if not isinstance(payload, dict):
        raise ValueError("resposta da criação de chave não é um objeto")
    secret = payload.get("key")
    data = payload.get("data")
    key_hash = data.get("hash") if isinstance(data, dict) else None
    if not isinstance(secret, str) or not secret:
        raise ValueError("resposta da criação de chave sem `key`")
    if not isinstance(key_hash, str) or not key_hash:
        raise ValueError("resposta da criação de chave sem `data.hash`")
    return key_hash, secret


class SecretBox:
    """Cifra/decifra os segredos das chaves espelho. A chave de cifra vem da
    env OPENROUTER_KEYS_ENCRYPTION_KEY (gerada com Fernet.generate_key()).
    Levanta ValueError na construção se ela for inválida — main.py desliga o
    mecanismo no boot em vez de descobrir no meio de uma request."""

    def __init__(self, fernet_key: str) -> None:
        try:
            self._fernet = Fernet(fernet_key.encode() if isinstance(fernet_key, str) else fernet_key)
        except Exception as e:
            # só o tamanho e o formato esperado, nunca o valor
            size = len(fernet_key) if isinstance(fernet_key, (str, bytes)) else 0
            raise ValueError(
                f"OPENROUTER_KEYS_ENCRYPTION_KEY inválida: tem {size} caracteres, "
                f"o esperado são 44 em base64 url-safe terminando em '=' ({e})"
            ) from e

    def encrypt(self, secret: str) -> str:
        return self._fernet.encrypt(secret.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as e:
            # chave de cifra trocada sem recriar as chaves espelho
            raise ValueError("segredo não decifra com a OPENROUTER_KEYS_ENCRYPTION_KEY atual") from e
