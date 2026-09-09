"""Testes de apply_key_image_defaults e apply_stack_image_defaults: os defaults
de geração de imagem por CHAVE (api_keys, migration 0063) e por STACK (stacks,
migration 0062), e a precedência entre eles, em /v1/images/generations. Rodar de
docker/gateway/:

    SUPABASE_URL=x SUPABASE_SERVICE_ROLE_KEY=y python3 -m pytest test_image_defaults.py

Mesmas env vars e mesmo motivo de test_sampling_defaults.py (main.py as lê
incondicionalmente no import). Nada aqui toca Supabase ou rede: a função é pura
sobre os dois dicts.
"""

import os

os.environ.setdefault("SUPABASE_URL", "https://example.invalid")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

from main import apply_key_image_defaults, apply_stack_image_defaults


def _apply_all(body, entry):
    """As duas camadas na ordem em que images_generations as chama. Testar por
    aqui, e não chamando cada uma solta, é o que faz a PRECEDÊNCIA ser o objeto
    do teste — trocar a ordem no call site tem que quebrar algo."""
    apply_key_image_defaults(body, entry)
    apply_stack_image_defaults(body, entry)


def _stack(id="s1", plan="go", category="image", **overrides):
    return {"id": id, "plan": plan, "category": category, **overrides}


def _entry(stack=None, **key_overrides):
    return {
        "stack_id": stack["id"] if stack else None,
        "stacks": [stack] if stack else [],
        **key_overrides,
    }


# ---------- 1. precedência: o que o cliente mandou ganha ----------


def test_cliente_explicito_ganha_do_default_da_stack():
    entry = _entry(
        stack=_stack(
            default_image_size="1024x1536",
            default_image_steps=8,
            default_image_guidance_scale=7.5,
        )
    )
    body = {"prompt": "x", "size": "1536x1024", "steps": 2, "guidance_scale": 1.0}
    apply_stack_image_defaults(body, entry)
    assert body["size"] == "1536x1024"
    assert body["steps"] == 2
    assert body["guidance_scale"] == 1.0


def test_campo_ausente_recebe_o_default_da_stack():
    entry = _entry(
        stack=_stack(
            default_image_size="1024x1536",
            default_image_steps=6,
            default_image_guidance_scale=3.5,
        )
    )
    body = {"prompt": "x"}
    apply_stack_image_defaults(body, entry)
    assert body["size"] == "1024x1536"
    assert body["steps"] == 6
    assert body["guidance_scale"] == 3.5


def test_campos_sao_independentes():
    """Um default configurado não arrasta os outros: a stack pode fixar só o
    tamanho e deixar steps/guidance no default do pod."""
    entry = _entry(stack=_stack(default_image_size="1536x1024"))
    body = {"prompt": "x"}
    apply_stack_image_defaults(body, entry)
    assert body["size"] == "1536x1024"
    assert "steps" not in body
    assert "guidance_scale" not in body


# ---------- 2. ausência de configuração não inventa valor ----------


def test_stack_sem_defaults_nao_mexe_no_corpo():
    """Sem coluna preenchida, quem decide continua sendo o pod (STEPS,
    GUIDANCE_SCALE, DEFAULT_SIZE) — o gateway não pode antecipar esses valores,
    senão uma mudança de default no pod deixaria de valer."""
    entry = _entry(stack=_stack())
    body = {"prompt": "x"}
    apply_stack_image_defaults(body, entry)
    assert body == {"prompt": "x"}


def test_default_nulo_e_tratado_como_ausente():
    entry = _entry(
        stack=_stack(
            default_image_size=None,
            default_image_steps=None,
            default_image_guidance_scale=None,
        )
    )
    body = {"prompt": "x"}
    apply_stack_image_defaults(body, entry)
    assert body == {"prompt": "x"}


def test_sem_stack_resolvivel_nao_estoura():
    """Mesmo contrato de apply_stack_sampling_defaults: chave sem stack
    resolvível sai sem tocar no corpo, em vez de levantar."""
    body = {"prompt": "x"}
    apply_stack_image_defaults(body, _entry(stack=None))
    assert body == {"prompt": "x"}


# ---------- 3. valores de fronteira que a 0062 permite ----------


def test_guidance_scale_zero_e_aplicado():
    """0 é um valor válido (a faixa é 0..20) e cai na armadilha clássica do
    `if not valor`: o guard tem que ser `is not None`, senão guidance_scale=0
    silenciosamente não é aplicado."""
    entry = _entry(stack=_stack(default_image_guidance_scale=0))
    body = {"prompt": "x"}
    apply_stack_image_defaults(body, entry)
    assert body["guidance_scale"] == 0


def test_steps_no_teto_da_migration():
    entry = _entry(stack=_stack(default_image_steps=8))
    body = {"prompt": "x"}
    apply_stack_image_defaults(body, entry)
    assert body["steps"] == 8


# ---------- 4. precedência entre chave e stack ----------


def test_chave_ganha_da_stack():
    entry = _entry(
        stack=_stack(
            default_image_size="1024x1024",
            default_image_steps=2,
            default_image_guidance_scale=1.0,
        ),
        default_image_size="1024x1536",
        default_image_steps=7,
        default_image_guidance_scale=9.5,
    )
    body = {"prompt": "x"}
    _apply_all(body, entry)
    assert body["size"] == "1024x1536"
    assert body["steps"] == 7
    assert body["guidance_scale"] == 9.5


def test_cliente_ganha_de_chave_e_stack():
    entry = _entry(
        stack=_stack(default_image_steps=2),
        default_image_steps=7,
    )
    body = {"prompt": "x", "steps": 3}
    _apply_all(body, entry)
    assert body["steps"] == 3


def test_stack_preenche_o_que_a_chave_nao_define():
    """As camadas são por CAMPO, não por bloco: uma chave que só fixa o tamanho
    não derruba os steps que a stack configurou."""
    entry = _entry(
        stack=_stack(default_image_steps=6, default_image_guidance_scale=4.0),
        default_image_size="1536x1024",
    )
    body = {"prompt": "x"}
    _apply_all(body, entry)
    assert body["size"] == "1536x1024"
    assert body["steps"] == 6
    assert body["guidance_scale"] == 4.0


def test_guidance_scale_zero_na_chave_ganha_da_stack():
    """O par do teste de fronteira acima, agora atravessando as duas camadas:
    com um guard `if not valor` na camada de chave, o 0 dela seria ignorado e a
    stack venceria — invertendo a precedência no único valor em que o bug é
    invisível."""
    entry = _entry(
        stack=_stack(default_image_guidance_scale=7.5),
        default_image_guidance_scale=0,
    )
    body = {"prompt": "x"}
    _apply_all(body, entry)
    assert body["guidance_scale"] == 0


def test_chave_sem_override_cai_na_stack():
    entry = _entry(stack=_stack(default_image_size="1024x1536"))
    body = {"prompt": "x"}
    _apply_all(body, entry)
    assert body["size"] == "1024x1536"


def test_chave_com_override_e_stack_sem_nada():
    """Uma chave pode configurar imagem numa stack que não configurou nada —
    apply_stack_image_defaults sai sem tocar no corpo e o valor da chave fica."""
    entry = _entry(stack=_stack(), default_image_steps=5)
    body = {"prompt": "x"}
    _apply_all(body, entry)
    assert body["steps"] == 5
