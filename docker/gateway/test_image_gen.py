"""Testes do módulo puro image_gen — sem httpx, sem fastapi, sem rede."""

import base64
from datetime import datetime, timedelta, timezone

import pytest

import image_gen

PNG = b"\x89PNG\r\n\x1a\n" + b"resto do arquivo"
JPEG = b"\xff\xd8\xff\xe0" + b"resto"
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"resto"

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
STACK = "11111111-1111-1111-1111-111111111111"
BATCH = "22222222-2222-2222-2222-222222222222"

CTX = dict(
    batch_id=BATCH,
    account_id="acc",
    stack_id=STACK,
    api_key_id="key",
    machine_id="mach",
    path="images/generations",
    model="flux2-klein-4b",
    retention_days=30,
)


def _payload(*blobs: bytes, meta=None, seeds=None):
    data = []
    for i, blob in enumerate(blobs):
        item = {"b64_json": base64.b64encode(blob).decode()}
        if seeds is not None:
            item["seed"] = seeds[i]
        data.append(item)
    payload = {"created": 1, "data": data}
    if meta is not None:
        payload["meta"] = meta
    return payload


# ---------- detect_content_type ----------


def test_detecta_png():
    assert image_gen.detect_content_type(PNG) == ("image/png", "png")


def test_detecta_jpeg():
    assert image_gen.detect_content_type(JPEG) == ("image/jpeg", "jpg")


def test_detecta_webp():
    assert image_gen.detect_content_type(WEBP) == ("image/webp", "webp")


def test_riff_que_nao_e_webp_nao_passa_por_webp():
    # RIFF é container genérico (wav, avi): só os bytes 8..12 desempatam
    assert image_gen.detect_content_type(b"RIFF____WAVEfmt ")[0] == "application/octet-stream"


def test_formato_desconhecido_nao_vira_png_chutado():
    content_type, ext = image_gen.detect_content_type(b"conteudo qualquer")
    assert content_type == "application/octet-stream"
    assert ext == "bin"


def test_bytes_insuficientes_nao_estouram():
    assert image_gen.detect_content_type(b"") == ("application/octet-stream", "bin")
    assert image_gen.detect_content_type(b"RI") == ("application/octet-stream", "bin")


# ---------- storage_path ----------


def test_storage_path_tem_stack_data_e_indice():
    path = image_gen.storage_path(
        stack_id=STACK, batch_id=BATCH, index=2, ext="png", now=NOW
    )
    assert path == f"{STACK}/2026-09-03/{BATCH}-2.png"


def test_storage_path_nao_repete_o_nome_do_bucket():
    path = image_gen.storage_path(
        stack_id=STACK, batch_id=BATCH, index=0, ext="png", now=NOW
    )
    assert not path.startswith("images/")


def test_storage_path_e_estavel_para_o_mesmo_batch():
    # é o que faz o retry de upload ser idempotente em vez de duplicar objeto
    args = dict(stack_id=STACK, batch_id=BATCH, index=0, ext="png", now=NOW)
    assert image_gen.storage_path(**args) == image_gen.storage_path(**args)


# ---------- expires_at ----------


def test_expires_at_soma_os_dias_de_retencao():
    assert image_gen.expires_at(NOW, 30) == NOW + timedelta(days=30)


# ---------- request_meta ----------


def test_request_meta_le_size_composto():
    meta = image_gen.request_meta({"prompt": "gato", "size": "1024x1536"})
    assert (meta["width"], meta["height"]) == (1024, 1536)
    assert meta["prompt"] == "gato"


def test_request_meta_com_size_invalido_nao_estoura():
    meta = image_gen.request_meta({"size": "grande"})
    assert (meta["width"], meta["height"]) == (None, None)


def test_request_meta_ignora_corpo_nao_dict():
    assert image_gen.request_meta("nada") == {}


def test_request_meta_com_steps_lixo_vira_nulo():
    # metadado é acessório: não pode derrubar a gravação da imagem
    assert image_gen.request_meta({"steps": "quatro"})["steps"] is None


# ---------- plan_persistence: formato ----------


def test_gera_uma_linha_por_imagem():
    pending = image_gen.plan_persistence(_payload(PNG, JPEG), now=NOW, **CTX)
    assert len(pending) == 2
    assert [p.row["image_index"] for p in pending] == [0, 1]
    assert [p.content_type for p in pending] == ["image/png", "image/jpeg"]


def test_decodifica_o_b64_para_os_bytes_originais():
    pending = image_gen.plan_persistence(_payload(PNG), now=NOW, **CTX)
    assert pending[0].data == PNG
    assert pending[0].row["bytes"] == len(PNG)


def test_row_carrega_a_identidade_de_quem_gerou():
    row = image_gen.plan_persistence(_payload(PNG), now=NOW, **CTX)[0].row
    assert row["account_id"] == "acc"
    assert row["stack_id"] == STACK
    assert row["api_key_id"] == "key"
    assert row["machine_id"] == "mach"
    assert row["batch_id"] == BATCH


def test_storage_path_da_row_bate_com_o_do_pending():
    # o insert promete que o objeto existe naquele path: divergir aqui criaria
    # linha apontando pra lugar nenhum
    pending = image_gen.plan_persistence(_payload(PNG), now=NOW, **CTX)[0]
    assert pending.row["storage_path"] == pending.storage_path


def test_expires_at_vai_na_row_em_iso():
    row = image_gen.plan_persistence(_payload(PNG), now=NOW, **CTX)[0].row
    assert row["expires_at"] == (NOW + timedelta(days=30)).isoformat()


# ---------- plan_persistence: metadados ----------


def test_meta_do_pod_ganha_do_corpo():
    payload = _payload(PNG, meta={"prompt": "efetivo", "steps": 4})
    row = image_gen.plan_persistence(
        payload, fallback_meta={"prompt": "pedido", "steps": 99}, now=NOW, **CTX
    )[0].row
    assert row["prompt"] == "efetivo"
    assert row["steps"] == 4


def test_fallback_preenche_o_que_o_pod_omitiu():
    payload = _payload(PNG, meta={"prompt": "efetivo"})
    row = image_gen.plan_persistence(
        payload, fallback_meta={"prompt": "pedido", "steps": 8}, now=NOW, **CTX
    )[0].row
    assert row["prompt"] == "efetivo"
    assert row["steps"] == 8


def test_pod_sem_bloco_meta_cai_inteiro_no_fallback():
    # é o pod de versão antiga: tem que continuar gravando, não falhar
    row = image_gen.plan_persistence(
        _payload(PNG), fallback_meta={"prompt": "pedido"}, now=NOW, **CTX
    )[0].row
    assert row["prompt"] == "pedido"


def test_sem_meta_nenhum_os_campos_ficam_nulos():
    # o caso do /v1/images/edits com pod antigo: multipart nunca é parseado
    row = image_gen.plan_persistence(_payload(PNG), now=NOW, **CTX)[0].row
    assert row["prompt"] is None
    assert row["seed"] is None


def test_seed_do_item_ganha_da_seed_do_meta():
    # com n>1 cada imagem tem a sua; o valor de topo descreveria só a primeira
    payload = _payload(PNG, JPEG, meta={"seed": 1}, seeds=[10, 20])
    rows = [p.row for p in image_gen.plan_persistence(payload, now=NOW, **CTX)]
    assert [r["seed"] for r in rows] == [10, 20]


def test_seed_no_teto_de_64_bits_e_preservada():
    payload = _payload(PNG, seeds=[image_gen.SEED_MAX])
    assert image_gen.plan_persistence(payload, now=NOW, **CTX)[0].row["seed"] == image_gen.SEED_MAX


def test_seed_acima_de_64_bits_vira_nula_em_vez_de_estourar_o_insert():
    payload = _payload(PNG, seeds=[image_gen.SEED_MAX + 1])
    assert image_gen.plan_persistence(payload, now=NOW, **CTX)[0].row["seed"] is None


def test_seed_negativa_vira_nula():
    payload = _payload(PNG, seeds=[-1])
    assert image_gen.plan_persistence(payload, now=NOW, **CTX)[0].row["seed"] is None


# ---------- plan_persistence: respostas malformadas ----------


def test_payload_sem_data_e_recusado():
    with pytest.raises(image_gen.MalformedImageResponse):
        image_gen.plan_persistence({"created": 1}, now=NOW, **CTX)


def test_data_vazia_e_recusada():
    with pytest.raises(image_gen.MalformedImageResponse):
        image_gen.plan_persistence({"data": []}, now=NOW, **CTX)


def test_item_sem_b64_json_e_recusado():
    with pytest.raises(image_gen.MalformedImageResponse):
        image_gen.plan_persistence({"data": [{"url": "x"}]}, now=NOW, **CTX)


def test_b64_invalido_e_recusado():
    with pytest.raises(image_gen.MalformedImageResponse):
        image_gen.plan_persistence({"data": [{"b64_json": "não é base64!"}]}, now=NOW, **CTX)


def test_b64_que_decodifica_para_vazio_e_recusado():
    with pytest.raises(image_gen.MalformedImageResponse):
        image_gen.plan_persistence({"data": [{"b64_json": ""}]}, now=NOW, **CTX)


def test_um_item_ruim_derruba_o_lote_inteiro():
    # a alternativa seria 200 com uma das imagens silenciosamente não gravada,
    # e o cliente não teria como saber qual
    payload = {"data": [{"b64_json": base64.b64encode(PNG).decode()}, {"b64_json": "@@@"}]}
    with pytest.raises(image_gen.MalformedImageResponse):
        image_gen.plan_persistence(payload, now=NOW, **CTX)


def test_resposta_que_nao_e_objeto_e_recusada():
    with pytest.raises(image_gen.MalformedImageResponse):
        image_gen.plan_persistence([1, 2, 3], now=NOW, **CTX)
