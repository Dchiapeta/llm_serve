#!/bin/bash
set -euo pipefail

# Gera docker/image/requirements.lock a partir do requirements.in, resolvendo
# contra a MESMA imagem base que o Dockerfile usa.
#
# Por que existe, em vez de um `pip freeze` na mão: o lock precisa conter só o
# DELTA em relação à base. Um freeze completo traria torch, torchvision e as
# ~60 libs que já vêm na imagem — e o `pip install --no-deps` do Dockerfile
# reinstalaria o torch a partir de PyPI, trocando a build de CUDA da base por
# uma wheel genérica de 2,5 GB.
#
# Rode DEPOIS de mexer no requirements.in e ANTES do build final. O lock é o
# artefato versionado; o requirements.in é só a declaração de intenção.
#
#   cd docker/image && ./lock-deps.sh

BASE="pytorch/pytorch:2.13.0-cuda12.6-cudnn9-runtime@sha256:6acf597eeb8e376a96580dde4952f37cc017fef732bb40bfc73f28f25e3f64b4"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "resolvendo contra ${BASE}"

docker run --rm --platform linux/amd64 \
  -v "${HERE}:/work" -w /work \
  "${BASE}" \
  bash -c '
    set -e
    # --break-system-packages: a base marca o Python do sistema como
    # externally-managed (PEP 668, /usr/lib/python3.12/EXTERNALLY-MANAGED) e sem
    # a flag TODO pip install falha. Não é gambiarra: a própria imagem instalou
    # o torch com pip em /usr/local/lib/python3.12/dist-packages, que é onde
    # estes pacotes também vão. Nada gerenciado pelo apt é tocado.
    PIP="pip install --break-system-packages"
    pip freeze --all > /tmp/antes.txt
    ${PIP} -q -r requirements.in
    pip freeze --all > /tmp/depois.txt
    # comm -13: linhas só no "depois" — pacotes novos e versões trocadas. É
    # exatamente o conjunto que o Dockerfile precisa instalar com --no-deps.
    comm -13 <(sort /tmp/antes.txt) <(sort /tmp/depois.txt)
  ' > "${HERE}/requirements.lock.tmp"

{
  echo "# GERADO por ./lock-deps.sh — não editar à mão."
  echo "# Delta sobre ${BASE}"
  echo "# Regerar após qualquer mudança em requirements.in."
  cat "${HERE}/requirements.lock.tmp"
} > "${HERE}/requirements.lock"

rm -f "${HERE}/requirements.lock.tmp"

# Guarda: nada que venha da base pode entrar no lock. Se `torch` aparecer aqui,
# alguma dependência pediu uma versão diferente da que a base traz, e o
# `pip install --no-deps` do Dockerfile substituiria a build de CUDA da base por
# uma wheel genérica de PyPI — 2,5 GB de download e, no pior caso, um container
# que sobe sem enxergar a GPU. Falhar alto aqui é muito mais barato que
# descobrir isso no boot da A40.
if grep -qiE '^(torch|torchvision|torchaudio|triton|nvidia-)' "${HERE}/requirements.lock"; then
  echo
  echo "ERRO: o lock contém pacote que deveria vir da imagem base:" >&2
  grep -iE '^(torch|torchvision|torchaudio|triton|nvidia-)' "${HERE}/requirements.lock" >&2
  echo "Fixe a versão do pacote que forçou o upgrade no requirements.in." >&2
  exit 1
fi

echo
echo "requirements.lock ($(grep -vc '^#' "${HERE}/requirements.lock") pacotes):"
grep -v '^#' "${HERE}/requirements.lock"
