# Imagem do pod de GERAÇÃO DE IMAGEM: servidor diffusers + o MESMO agent de
# autenticação/telemetria dos pods de LLM.
#
# Base pinada por DIGEST, não só por tag. A tag 2.13.0-cuda12.6-cudnn9-runtime
# é mutável — o mantenedor pode republicá-la — e um teste de performance que não
# sabe qual base rodou não vale nada. Digest verificado no Docker Hub em
# 03/09/2026 (single-arch linux/amd64, que é a plataforma do build).
#
# Por que a 2.13.0 e não a 2.14.0: a 2.14.0-cuda12.6-cudnn9-runtime existe, mas
# foi publicada em 02/09/2026. A 2.13.0 é de 08/07/2026 e é a escolha madura.
FROM pytorch/pytorch:2.13.0-cuda12.6-cudnn9-runtime@sha256:6acf597eeb8e376a96580dde4952f37cc017fef732bb40bfc73f28f25e3f64b4

WORKDIR /opt/agent

# requirements.lock, e não requirements.in: o lock tem todas as transitivas
# pinadas e é gerado por ./lock-deps.sh contra ESTA base. --no-deps porque o
# lock já é fechado — deixar o resolvedor solto aqui reintroduziria a variação
# que o lock existe para eliminar.
#
# O lock NÃO inclui torch/torchvision: eles vêm da base com a build de CUDA
# certa, e reinstalá-los de PyPI traria uma wheel genérica de 2,5 GB.
#
# --break-system-packages: a base marca o Python do sistema como
# externally-managed (PEP 668, /usr/lib/python3.12/EXTERNALLY-MANAGED) e sem a
# flag QUALQUER pip install falha o build. A própria imagem base instalou o
# torch com pip em /usr/local/lib/python3.12/dist-packages, que é exatamente
# onde estes pacotes vão — nada gerenciado pelo apt é tocado.
COPY image/requirements.lock .
RUN pip install --no-cache-dir --no-deps --break-system-packages -r requirements.lock

# O agent é compartilhado com o pod de vLLM (docker/agent/). Arquivo a arquivo
# de propósito, pelo mesmo motivo do docker/Dockerfile: um COPY agent/*.py
# levaria os testes para dentro da imagem.
COPY agent/main.py .
COPY agent/proxy_policy.py .
COPY agent/usage_norm.py .

# O servidor de imagem e a política/fila dele. test_*.py ficam fora.
COPY image/server.py .
COPY image/policy.py .

# Smoke check de import, em tempo de BUILD.
#
# `--no-deps` acima transforma qualquer suposição errada sobre o que a base
# traz num ImageNotFound/ImportError no BOOT — e o boot acontece numa A40
# alugada, depois de baixar 16 GB de pesos. Esta linha move a falha para cá,
# onde custa 5 segundos.
#
# A lista cobre a cadeia inteira, inclusive as transitivas que o --no-deps NÃO
# instalaria se um dia deixassem de vir na base (PIL vem via torchvision;
# anyio 4.15 não depende mais de sniffio, mas httpx/starlette dependem de
# anyio).
RUN python3 -c "import torch, torchvision, PIL, numpy; import diffusers, transformers, accelerate, safetensors, sentencepiece; import fastapi, uvicorn, multipart, httpx, pydantic, anyio, anyio.to_thread, starlette; from diffusers import Flux2KleinPipeline; from transformers import Qwen3ForCausalLM; print('smoke check ok:', torch.__version__, diffusers.__version__, transformers.__version__)"

COPY image/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Única porta exposta é a do agent; o servidor de imagem fica interno em 8001,
# atrás da autenticação.
EXPOSE 8000

# Mesmo nome de variável do pod de vLLM: é o base_url do httpx do agent
# (docker/agent/main.py:VLLM_URL). Manter o nome é o que permite reusar o agent
# sem uma linha de mudança nele.
ENV VLLM_URL=http://127.0.0.1:8001

ENTRYPOINT ["/entrypoint.sh"]
