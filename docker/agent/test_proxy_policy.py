"""Testes da política de proxy do agent — módulo puro.

    python3 -m pytest test_proxy_policy.py

O que está sendo protegido aqui é o isolamento de prefix cache entre tenants
que dividem o mesmo processo vLLM. Um bug silencioso nestas funções não
aparece como erro: aparece como um tenant lendo o tempo de resposta do outro.
"""

import asyncio
import json

import pytest

from proxy_policy import (
    ALLOWED_V1,
    MAX_BODY_BYTES_BY_PATH,
    SALT_EXEMPT_PATHS,
    SALTED_PATHS,
    BodyTooLarge,
    UnparseableBody,
    apply_cache_salt,
    declared_length_exceeds,
    max_body_bytes,
    merge_key_entry,
    prepare_proxy_body,
    read_body_capped,
    salt_ident,
    tenant_cache_salt,
    upstream_content_type_for,
)

SECRET = "segredo-do-pod"
STACK_A = "11111111-1111-1111-1111-111111111111"
STACK_B = "22222222-2222-2222-2222-222222222222"


def _salt(entry: dict, secret: str = SECRET) -> str:
    return tenant_cache_salt(salt_ident(entry), secret)


def _entry(**over) -> dict:
    row = {"key_hash": "h1", "stack_id": STACK_A}
    row.update(over)
    return row


# ---------- granularidade do salt ----------


def test_stacks_diferentes_tem_salts_diferentes():
    """É o teste que define o produto: dois tenants no mesmo pod não podem
    compartilhar bloco de KV nenhum."""
    assert _salt(_entry(stack_id=STACK_A)) != _salt(_entry(stack_id=STACK_B))


def test_chaves_diferentes_da_mesma_stack_tem_o_MESMO_salt():
    """De onde vem quase todo o ganho: dois devs da mesma stack compartilham o
    prefixo de ~26k tokens de system+tools do Claude Code. Salt por chave
    fragmentaria isso em N cópias."""
    a = _entry(key_hash="hash-do-dev-1", stack_id=STACK_A)
    b = _entry(key_hash="hash-do-dev-2", stack_id=STACK_A)
    assert _salt(a) == _salt(b)


def test_o_salt_e_derivado_do_segredo_do_pod():
    """Pods diferentes derivam salts diferentes pro mesmo stack_id — não que
    isso isole nada (cada pod tem seu próprio cache), mas garante que o salt
    não é previsível por quem só conhece o stack_id."""
    assert _salt(_entry(), "outro-segredo") != _salt(_entry(), SECRET)


def test_o_salt_nao_ecoa_o_stack_id():
    """O vLLM pode devolver o corpo da request num erro de validação do
    pydantic; com o ident cru isso vazaria o stack_id de volta pro cliente."""
    assert STACK_A not in _salt(_entry())


def test_fallback_kh_quando_nunca_houve_stack_id():
    """Produtor de payload antigo (gateway/painel sem o plumbing). Isola
    igual — chave é por conta —, mas fragmenta e não sobrevive a rotação."""
    assert salt_ident({"key_hash": "abc"}) == "kh:abc"
    assert salt_ident({"key_hash": "abc", "stack_id": None}) == "kh:abc"
    # e continua sendo um ident DISTINTO por chave
    assert _salt({"key_hash": "abc"}) != _salt({"key_hash": "def"})


# ---------- anti-thrash do merge ----------


def test_ciclo_upsert_sync_upsert_nao_troca_o_salt():
    """O cenário que motiva o carry-over: os produtores de payload divergem, e
    o do meio (um /admin/sync-keys de versão antiga) não manda stack_id. Sem
    carry-over o salt alternaria entre stack_id e kh:* a cada re-sync,
    INVALIDANDO todo o cache do tenant a cada flip — pagando o custo de VRAM
    do caching sem colher o benefício."""
    com_stack = {"api_key_id": "k1", "stack_id": STACK_A}
    sem_stack = {"api_key_id": "k1"}  # produtor antigo: campo AUSENTE

    e1 = merge_key_entry(None, com_stack, "h1")
    e2 = merge_key_entry(e1, sem_stack, "h1")
    e3 = merge_key_entry(e2, com_stack, "h1")

    assert _salt(e1) == _salt(e2) == _salt(e3)
    assert e2["stack_id"] == STACK_A


def test_stack_id_none_explicito_sobrescreve():
    """Ausente preserva, PRESENTE sobrescreve — é como uma stack desvinculada
    consegue voltar pro ramo kh:."""
    prev = merge_key_entry(None, {"stack_id": STACK_A}, "h1")
    depois = merge_key_entry(prev, {"stack_id": None}, "h1")
    assert depois["stack_id"] is None
    assert salt_ident(depois) == "kh:h1"


def test_expires_at_nao_tem_carry_over():
    """Carry-over em expires_at seria bug de SEGURANÇA: um None novo precisa
    poder limpar uma expiração antiga (o agent barra chave expirada por conta
    própria, pra fechar o bypass de quem chama o pod direto)."""
    prev = merge_key_entry(None, {"expires_at": "2026-01-01T00:00:00Z"}, "h1")
    depois = merge_key_entry(prev, {"stack_id": STACK_A}, "h1")
    assert depois["expires_at"] is None


def test_merge_preenche_o_key_hash_e_os_defaults():
    e = merge_key_entry(None, {}, "h9")
    assert e["key_hash"] == "h9"
    assert e["key_prefix"] == "?" and e["account_name"] == "?"
    assert e["api_key_id"] is None and e["stack_id"] is None


# ---------- injeção / descarte do cache_salt ----------


def test_cache_salt_do_cliente_e_sempre_descartado():
    """A defesa que importa. O pod é alcançável direto pela URL pública do
    RunPod: sem este pop, um tenant manda o salt da vítima e colide de
    propósito com o cache dela."""
    for path in sorted(SALTED_PATHS | SALT_EXEMPT_PATHS):
        body = apply_cache_salt({"cache_salt": "do-atacante"}, path, "meu-salt", True)
        assert body.get("cache_salt") != "do-atacante"


def test_descarta_mesmo_com_o_salting_desligado():
    """Pod sem PREFIX_CACHE_ISOLATION: o campo não pode existir pra ninguém,
    em vez de existir só pra quem souber mandá-lo."""
    body = apply_cache_salt({"cache_salt": "do-atacante"}, "chat/completions", None, False)
    assert "cache_salt" not in body


def test_injeta_nos_paths_generativos():
    for path in sorted(SALTED_PATHS):
        body = apply_cache_salt({"model": "m"}, path, "meu-salt", True)
        assert body["cache_salt"] == "meu-salt"


def test_nao_injeta_em_path_isento():
    body = apply_cache_salt({"input": "x"}, "embeddings", "meu-salt", True)
    assert "cache_salt" not in body


def test_nao_injeta_sem_salt_derivado():
    """Pod com a env ligada mas sem AGENT_ADMIN_SECRET: não há salt possível.
    Melhor não injetar nada do que injetar um valor constante, que uniria
    TODOS os tenants no mesmo namespace de cache."""
    body = apply_cache_salt({"model": "m"}, "chat/completions", None, True)
    assert "cache_salt" not in body


# ---------- guarda contra endpoint novo sem classificação ----------


def test_todo_post_da_allowlist_esta_classificado():
    """Uma allowlist positiva sozinha falha ABERTA: alguém adiciona um endpoint
    generativo novo em ALLOWED_V1, ninguém lembra do salt, e ele passa sem
    isolamento — silenciosamente. Este teste quebra o build nesse dia e
    obriga uma decisão consciente: salgado ou isento."""
    posts = {p for p, methods in ALLOWED_V1.items() if "POST" in methods}
    assert posts == SALTED_PATHS | SALT_EXEMPT_PATHS


def test_salgado_e_isento_nao_se_sobrepoem():
    assert not (SALTED_PATHS & SALT_EXEMPT_PATHS)


# ---------- reescrita do corpo antes do proxy ----------


def _prepare(body, path="chat/completions", *, salt_enabled=True, secret=SECRET):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    out, is_stream = prepare_proxy_body(
        raw, path, _entry(), salt_enabled=salt_enabled, secret=secret
    )
    try:
        return json.loads(out), is_stream
    except Exception:
        return out, is_stream


def test_requisicao_nao_streaming_tambem_recebe_o_salt():
    """Regressão de verdade: o json.dumps ficava DENTRO do `if is_stream`, então
    o caminho não-streaming reenviava o corpo ORIGINAL e a injeção do salt era
    perdida em silêncio — caching ligado, sem isolamento, sem erro."""
    body, is_stream = _prepare({"model": "m"})
    assert is_stream is False
    assert body["cache_salt"] == _salt(_entry())


def test_stream_options_null_nao_crasha_e_ganha_include_usage():
    """setdefault num "stream_options": null devolveria None e o .setdefault
    seguinte estouraria AttributeError. Antes isso era engolido por um
    `except Exception: pass`; hoje derrubaria a request."""
    body, is_stream = _prepare({"stream": True, "stream_options": None})
    assert is_stream is True
    assert body["stream_options"]["include_usage"] is True


def test_include_usage_do_cliente_e_respeitado():
    body, _ = _prepare({"stream": True, "stream_options": {"include_usage": False}})
    assert body["stream_options"]["include_usage"] is False


def test_corpo_nao_json_em_path_salgado_falha_fechado():
    for raw in (b"nao sou json", b"[1,2,3]"):
        try:
            prepare_proxy_body(
                raw, "chat/completions", _entry(), salt_enabled=True, secret=SECRET
            )
        except UnparseableBody:
            continue
        raise AssertionError(f"deveria ter falhado fechado para {raw!r}")


def test_corpo_nao_json_passa_em_path_isento():
    """embeddings não aceita cache_salt; quem valida o formato ali é o vLLM."""
    out, is_stream = prepare_proxy_body(
        b"nao sou json", "embeddings", _entry(), salt_enabled=True, secret=SECRET
    )
    assert out == b"nao sou json" and is_stream is False


def test_corpo_nao_json_passa_com_o_salting_desligado():
    """Sem caching ligado não há nada a proteger — não introduzir um 400 novo
    num pod que hoje funciona."""
    out, _ = prepare_proxy_body(
        b"nao sou json", "chat/completions", _entry(), salt_enabled=False, secret=SECRET
    )
    assert out == b"nao sou json"


# ---------- rotas de imagem (pod de difusão) ----------


def test_rotas_de_imagem_estao_na_allowlist():
    """Sem elas o próprio agent devolve 404 antes de chegar ao servidor de
    difusão — o pod subiria saudável e não atenderia nada."""
    assert ALLOWED_V1.get("images/generations") == {"POST"}
    assert ALLOWED_V1.get("images/edits") == {"POST"}


def test_rotas_de_imagem_sao_isentas_de_salt():
    """Difusão não tem KV cache: não há canal lateral de prefixo para isolar."""
    assert "images/generations" in SALT_EXEMPT_PATHS
    assert "images/edits" in SALT_EXEMPT_PATHS
    assert not (SALTED_PATHS & {"images/generations", "images/edits"})


def test_corpo_multipart_atravessa_intacto():
    """O /v1/images/edits é multipart, e o corpo NÃO pode ser reserializado: as
    partes são delimitadas pelo boundary declarado no Content-Type, e qualquer
    reescrita quebraria o parse do outro lado. Este teste fixa o comportamento
    de que prepare_proxy_body devolve os bytes originais."""
    raw = (
        b"--b0undary\r\n"
        b'Content-Disposition: form-data; name="prompt"\r\n\r\n'
        b"um gato\r\n"
        b"--b0undary\r\n"
        b'Content-Disposition: form-data; name="image[]"; filename="a.png"\r\n'
        b"Content-Type: image/png\r\n\r\n"
        b"\x89PNG\r\n\x1a\n\x00\x00\r\n"
        b"--b0undary--\r\n"
    )
    out, is_stream = prepare_proxy_body(
        raw, "images/edits", _entry(), salt_enabled=True, secret=SECRET
    )
    assert out == raw, "corpo multipart foi reescrito — o boundary não sobrevive a isso"
    assert is_stream is False


def test_multipart_nao_recebe_cache_salt_injetado():
    """Mesmo com o salting ligado no pod, um corpo que não é objeto JSON não
    pode ganhar campo nenhum."""
    raw = b"--b\r\nContent-Disposition: form-data; name=\"prompt\"\r\n\r\nx\r\n--b--\r\n"
    out, _ = prepare_proxy_body(
        raw, "images/edits", _entry(), salt_enabled=True, secret=SECRET
    )
    assert b"cache_salt" not in out


# ---------- normalização do Content-Type de upstream ----------


def _ct(raw: str) -> str:
    return upstream_content_type_for(raw)


def test_nao_multipart_e_forcado_para_json():
    """Comportamento histórico que NÃO pode regredir: cliente que manda corpo
    JSON declarando text/plain funciona porque o agent corrige o header. Sem
    isso ele passaria a tomar 422 do vLLM."""
    for raw in ("application/json", "text/plain", "application/json; charset=utf-8", ""):
        assert _ct(raw) == "application/json"


def test_multipart_preserva_o_boundary():
    """O boundary é o que delimita as partes — perdê-lo torna o corpo
    impossível de parsear."""
    assert _ct("multipart/form-data; boundary=XyZ123") == "multipart/form-data; boundary=XyZ123"


def test_multipart_maiusculo_e_normalizado_mas_o_boundary_nao():
    """Media type é case-insensitive (RFC 9110 §8.3.1), mas o request.form() do
    Starlette compara com o literal b"multipart/form-data" sem normalizar: um
    "Multipart/Form-Data" chega intacto e cai no ramo de form VAZIO, e a rota
    responde missing_image em vez de processar as imagens. Medido no container.

    O boundary tem que sobreviver com o case original — ele É case-sensitive."""
    assert _ct("Multipart/Form-Data; boundary=XyZ123") == "multipart/form-data; boundary=XyZ123"
    assert _ct("MULTIPART/FORM-DATA; boundary=AbC") == "multipart/form-data; boundary=AbC"


def test_multipart_sem_parametros():
    assert _ct("multipart/form-data") == "multipart/form-data"
    assert _ct("  Multipart/Form-Data  ") == "multipart/form-data"


# ---------- teto de corpo por rota ----------


async def _aiter(items):
    for i in items:
        yield i


def test_toda_rota_da_allowlist_tem_teto():
    """Guarda irmã do test_todo_post_da_allowlist_esta_classificado: rota nova
    em ALLOWED_V1 sem teto cairia no DEFAULT conservador e provavelmente daria
    413 em uso legítimo — ou, se alguém trocasse o default pelo maior valor,
    reabriria o buraco de memória. Ou o teto é decidido, ou o build quebra."""
    assert set(ALLOWED_V1) == set(MAX_BODY_BYTES_BY_PATH)


def test_teto_das_rotas_de_llm_tem_folga_sobre_o_do_gateway():
    """O gateway corta o corpo do CLIENTE em 8 MB e SÓ DEPOIS injeta model,
    system prompt da stack, RAG e stream_options — o corpo chega aqui maior do
    que entrou lá. Espelhar os 8 MB faria o agent recusar o que o gateway
    aprovou, com um 413 que o cliente não tem como agir."""
    assert max_body_bytes("chat/completions") > 8_000_000


def test_generations_e_muito_mais_apertado_que_edits():
    assert max_body_bytes("images/generations") < max_body_bytes("images/edits")


def test_teto_de_edits_cobre_o_maximo_do_pod():
    """4 arquivos de 15 MiB é o que IMAGE_MAX_REFERENCE_IMAGES e
    IMAGE_MAX_FILE_SIZE_MB permitem. Um teto abaixo disso recusaria no pod o
    que o gateway (image_proxy.max_edit_bytes) deixou passar."""
    assert max_body_bytes("images/edits") > 4 * 15 * 1024 * 1024


def test_rota_desconhecida_cai_no_teto_conservador():
    """Falhar apertado é recuperável; falhar largo é o buraco que o mapa fecha."""
    assert max_body_bytes("rota/que/nao/existe") == 256 * 1024


# ---------- corte por Content-Length declarado ----------


def test_content_length_acima_do_teto_e_recusado():
    assert declared_length_exceeds("100", 10) is True


def test_content_length_no_teto_passa():
    assert declared_length_exceeds("10", 10) is False


def test_content_length_ausente_nao_recusa():
    """Chunked não tem o header. Quem cobre esse caso é read_body_capped — o
    header só serve pra RECUSAR cedo, nunca pra ACEITAR."""
    assert declared_length_exceeds(None, 10) is False
    assert declared_length_exceeds("", 10) is False


def test_content_length_ilegivel_nao_recusa():
    assert declared_length_exceeds("abc", 10) is False
    assert declared_length_exceeds("-5", 10) is False


# ---------- leitura incremental ----------


def test_corpo_abaixo_do_teto_e_montado_inteiro():
    body = asyncio.run(read_body_capped(_aiter([b"ab", b"cd"]), "chat/completions", 10))
    assert body == b"abcd"


def test_corpo_exatamente_no_teto_passa():
    body = asyncio.run(read_body_capped(_aiter([b"abcde"]), "chat/completions", 5))
    assert body == b"abcde"


def test_corpo_acima_do_teto_levanta():
    with pytest.raises(BodyTooLarge) as e:
        asyncio.run(read_body_capped(_aiter([b"abc", b"def"]), "images/edits", 5))
    assert e.value.path == "images/edits"
    assert e.value.ceiling == 5


def test_corpo_vazio():
    assert asyncio.run(read_body_capped(_aiter([]), "models", 10)) == b""


def test_estouro_nao_le_o_corpo_inteiro():
    """O ponto da leitura incremental: a exceção sobe no chunk que cruza o
    teto, sem consumir o resto do stream. Com `await request.body()` seguido de
    len(), os 10 GB já teriam sido carregados quando a comparação rodasse."""
    consumidos = 0

    async def contando():
        nonlocal consumidos
        for _ in range(1000):
            consumidos += 1
            yield b"x" * 1024

    with pytest.raises(BodyTooLarge):
        asyncio.run(read_body_capped(contando(), "images/generations", 4096))
    assert consumidos <= 6  # ~5 chunks de 1 KiB pra cruzar 4 KiB, não 1000
