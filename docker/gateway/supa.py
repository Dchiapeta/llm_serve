"""
Cliente Supabase do gateway: consultas PostgREST (api_keys, machines,
lora_adapters) e signed URLs do Storage para os arquivos dos adapters.

Toda a comunicação usa a service role key — o gateway é o único componente
fora do painel com esse acesso; os pods nunca recebem credenciais Supabase.
"""

from datetime import datetime, timezone

import httpx

# Manter em sincronia com LORA_ALLOWED_FILES em docker/agent/main.py
# e lib/actions.ts — o agent rejeita qualquer arquivo fora desta lista.
LORA_REQUIRED_FILES = {"adapter_config.json", "adapter_model.safetensors"}
LORA_ALLOWED_FILES = LORA_REQUIRED_FILES | {
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "added_tokens.json",
    "chat_template.jinja",
}


class SupaClient:
    def __init__(self, supabase_url: str, service_role_key: str, lora_bucket: str = "loras"):
        headers = {
            "apikey": service_role_key,
            "Authorization": f"Bearer {service_role_key}",
            "Content-Type": "application/json",
        }
        self._rest = httpx.AsyncClient(
            base_url=f"{supabase_url}/rest/v1", headers=headers,
            timeout=httpx.Timeout(10.0),
        )
        self._storage = httpx.AsyncClient(
            base_url=f"{supabase_url}/storage/v1", headers=headers,
            timeout=httpx.Timeout(30.0),
        )
        self._supabase_url = supabase_url
        self._bucket = lora_bucket

    async def aclose(self):
        await self._rest.aclose()
        await self._storage.aclose()

    # ---------- api_keys ----------

    async def find_active_key(self, key_hash: str) -> dict | None:
        """Retorna {account_id, key_prefix, account_name, plan, system_prompt}
        da chave ativa, ou None."""
        r = await self._rest.get(
            "/api_keys",
            params={
                "key_hash": f"eq.{key_hash}",
                "status": "eq.active",
                "select": "account_id,key_prefix,key_hash,accounts(name,plan,system_prompt)",
                "limit": "1",
            },
        )
        r.raise_for_status()
        rows = r.json()
        if not rows:
            return None
        row = rows[0]
        account = row.get("accounts") or {}
        return {
            "account_id": row["account_id"],
            "key_prefix": row["key_prefix"],
            "key_hash": row["key_hash"],
            "account_name": account.get("name", "?"),
            "plan": account.get("plan", "VibeCoder"),
            "system_prompt": account.get("system_prompt"),
        }

    async def list_active_keys_for_account(self, account_id: str) -> list[dict]:
        """Entradas de chave ativas da conta, no formato do /admin/upsert-keys."""
        r = await self._rest.get(
            "/api_keys",
            params={
                "account_id": f"eq.{account_id}",
                "status": "eq.active",
                "select": "key_hash,key_prefix,accounts(name)",
            },
        )
        r.raise_for_status()
        return [
            {
                "key_hash": row["key_hash"],
                "key_prefix": row["key_prefix"],
                "account_name": (row.get("accounts") or {}).get("name", "?"),
            }
            for row in r.json()
        ]

    # ---------- machines ----------

    async def get_machine(self, machine_id: str) -> dict | None:
        r = await self._rest.get(
            "/machines",
            params={"id": f"eq.{machine_id}", "select": "*", "limit": "1"},
        )
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None

    async def list_running_machines(self) -> list[dict]:
        r = await self._rest.get(
            "/machines",
            params={
                "status": "eq.running",
                "public_url": "not.is.null",
                "select": "*",
                "order": "created_at.asc",
            },
        )
        r.raise_for_status()
        return r.json()

    async def list_running_machines_for_plan(self, plan: str) -> list[dict]:
        """Máquinas running cujo template serve o plano da conta.

        Sem este filtro, uma conta sem adapter cairia em QUALQUER máquina
        running (ver list_running_machines) — inofensivo com um único
        modelo base em produção, mas quebra assim que existir mais de um
        template/modelo simultâneo (ex: VibeCoder em Qwen3.5-9B e Pro/Max em
        Qwen3.6-35B). O join usa a FK machines.template_id → templates.id.
        """
        r = await self._rest.get(
            "/machines",
            params={
                "status": "eq.running",
                "public_url": "not.is.null",
                "select": "*,templates!inner(plan)",
                "templates.plan": f"eq.{plan}",
                "order": "created_at.asc",
            },
        )
        r.raise_for_status()
        return r.json()

    async def set_machine_status(self, machine_id: str, status: str) -> None:
        r = await self._rest.patch(
            "/machines",
            params={"id": f"eq.{machine_id}"},
            json={"status": status},
        )
        r.raise_for_status()

    async def touch_machine_activity(self, machine_id: str) -> None:
        r = await self._rest.patch(
            "/machines",
            params={"id": f"eq.{machine_id}"},
            json={"last_activity_at": datetime.now(timezone.utc).isoformat()},
        )
        r.raise_for_status()

    async def log_machine_event(self, machine_id: str, type_: str, message: str) -> None:
        """Espelho do logEvent do painel — eventos do lifecycle aparecem na UI."""
        r = await self._rest.post(
            "/machine_events",
            json={"machine_id": machine_id, "type": type_, "message": message},
        )
        r.raise_for_status()

    async def count_active_routes(self, machine_id: str) -> int:
        """Rotas ocupando slot LoRA na máquina (loading, loaded ou migrating)."""
        r = await self._rest.get(
            "/routing_state",
            params={
                "machine_id": f"eq.{machine_id}",
                "lora_status": "in.(loading,loaded,migrating)",
                "select": "account_id",
            },
            headers={"Prefer": "count=exact"},
        )
        r.raise_for_status()
        content_range = r.headers.get("content-range", "/0")
        return int(content_range.split("/")[-1])

    async def machine_lora_slots(self, machine_id: str) -> int | None:
        """Slots LoRA da máquina pela fórmula de capacidade (função SQL única,
        compartilhada com o painel). None = capacidade desconhecida."""
        r = await self._rest.post(
            "/rpc/machine_lora_slots", json={"p_machine_id": machine_id}
        )
        r.raise_for_status()
        return r.json()

    # ---------- lora_adapters ----------

    async def latest_ready_adapter(self, account_id: str) -> dict | None:
        """Adapter 'ready' mais recente da conta, confirmado contra o Storage.

        O status salvo no banco sozinho não é confiável: pode ter sido gravado
        fora da validação (bug já corrigido em registerLoraAdapter, que antes
        contava pastas do dashboard como se fossem os arquivos do adapter) ou
        os arquivos podem ter sumido do bucket depois do registro. Aqui
        confirmamos a presença real dos arquivos antes de devolver e
        invalidamos no banco qualquer linha que falhe, pra não ficar
        retentando o mesmo adapter quebrado a cada request.
        """
        r = await self._rest.get(
            "/lora_adapters",
            params={
                "account_id": f"eq.{account_id}",
                "status": "eq.ready",
                "select": "*",
                "order": "created_at.desc",
            },
        )
        r.raise_for_status()
        for row in r.json():
            names = await self._list_storage_names(row["storage_path"])
            if LORA_REQUIRED_FILES.issubset(names):
                return row
            await self.mark_adapter_invalid(row["id"])
        return None

    async def mark_adapter_invalid(self, adapter_id: str) -> None:
        r = await self._rest.patch(
            "/lora_adapters",
            params={"id": f"eq.{adapter_id}"},
            json={"status": "invalid"},
        )
        r.raise_for_status()

    # ---------- knowledge_chunks (RAG) ----------

    async def match_knowledge_chunks(
        self, account_id: str, query_embedding: list[float], top_k: int = 4
    ) -> list[str]:
        """Top-k chunks da base de conhecimento da conta por similaridade de
        cosseno (RPC match_knowledge_chunks, mesma função usada pelo painel)."""
        r = await self._rest.post(
            "/rpc/match_knowledge_chunks",
            json={
                "p_account_id": account_id,
                "p_query_embedding": query_embedding,
                "p_top_k": top_k,
            },
        )
        r.raise_for_status()
        return [row["content"] for row in r.json()]

    # ---------- storage (signed URLs) ----------

    async def _list_storage_names(self, storage_path: str) -> set[str]:
        """Nomes de arquivos reais no prefixo — entradas sem id são pastas e não contam."""
        r = await self._storage.post(
            f"/object/list/{self._bucket}",
            json={"prefix": storage_path, "limit": 100},
        )
        r.raise_for_status()
        return {f["name"] for f in r.json() if f.get("id")}

    async def signed_lora_files(self, storage_path: str, ttl_s: int = 600) -> list[dict]:
        """Lista o prefixo do adapter e assina só arquivos da whitelist PEFT."""
        all_names = await self._list_storage_names(storage_path)
        names = [n for n in all_names if n in LORA_ALLOWED_FILES]

        missing = LORA_REQUIRED_FILES - set(names)
        if missing:
            raise RuntimeError(
                f"adapter incompleto em {self._bucket}/{storage_path} — faltam: {', '.join(sorted(missing))}"
            )

        signed: list[dict] = []
        for name in names:
            resp = await self._storage.post(
                f"/object/sign/{self._bucket}/{storage_path}/{name}",
                json={"expiresIn": ttl_s},
            )
            resp.raise_for_status()
            # a API devolve um path relativo ("/object/sign/...?token=...")
            rel = resp.json()["signedURL"].lstrip("/")
            signed.append({"name": name, "url": f"{self._supabase_url}/storage/v1/{rel}"})
        return signed
