# Imagem: vLLM (servidor OpenAI-compatible) + agent de autenticação/telemetria
# Versão pinada: o load/unload dinâmico de LoRA depende do comportamento
# desta versão — não usar "latest".
FROM vllm/vllm-openai:v0.24.0

WORKDIR /opt/agent

COPY agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/main.py .
# proxy_policy é importado por main.py no topo — sem esta linha o pod sobe com
# ModuleNotFoundError e nunca fica saudável. Arquivo a arquivo de propósito:
# um COPY agent/*.py levaria os testes pra dentro da imagem.
COPY agent/proxy_policy.py .
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# única porta exposta é a do agent; o vLLM fica interno em 8001
EXPOSE 8000

ENV VLLM_URL=http://127.0.0.1:8001

ENTRYPOINT ["/entrypoint.sh"]
