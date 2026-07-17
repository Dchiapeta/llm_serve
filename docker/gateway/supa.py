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
        """Retorna {account_id, key_prefix, account_name, plan, system_prompt,
        stack_id, stacks} da chave ativa, ou None. Os stacks vêm embutidos (FK
        reversa accounts → stacks) pro roteamento base ser stack-aware sem
        query extra por request — o dict inteiro pega carona no key_cache do
        gateway. `stack_id` (coluna direta de api_keys, migration 0019)
        identifica QUAL dessas stacks é a da própria chave — sem ele (chave
        legada), o roteamento cai no heurístico por accounts.plan.

        `system_prompt` no nível de `accounts` (migration 0010) é só fallback
        legado — desde a migration 0020 cada stack tem o seu próprio
        `system_prompt`, embutido em cada item de `stacks`, resolvido por
        `resolve_key_stack` (main.py) no lugar do valor da conta inteira."""
        r = await self._rest.get(
            "/api_keys",
            params={
                "key_hash": f"eq.{key_hash}",
                "status": "eq.active",
                "select": "account_id,key_prefix,key_hash,stack_id,"
                "accounts(name,plan,system_prompt,"
                "stacks(id,machine_id,plan,slug,created_at,system_prompt))",
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
            "stack_id": row.get("stack_id"),
            "account_name": account.get("name", "?"),
            "plan": account.get("plan", "VibeCoder"),
            "system_prompt": account.get("system_prompt"),
            "stacks": account.get("stacks") or [],
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

    async def list_active_keys_for_machine(self, machine_id: str) -> list[dict]:
        """Chaves ativas vinculadas à máquina, no formato do /admin/upsert-keys.
        Espelho do select do syncMachineKeys (lib/actions.ts) — usado no
        re-sync pós-religada, já que o agent perde as chaves ao reiniciar."""
        r = await self._rest.get(
            "/api_keys",
            params={
                "machine_id": f"eq.{machine_id}",
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

    async def move_account_keys(
        self,
        account_id: str,
        from_machine_id: str,
        to_machine_id: str,
        stack_id: str | None = None,
    ) -> int:
        """Move as chaves ativas da conta de uma máquina pra outra (realocação
        automática). MOVE a linha existente — nunca cria/revoga como o
        migrateStack do painel, senão a plain key configurada no cliente do
        usuário deixaria de funcionar. PATCH condicional: só linhas ainda na
        origem mudam; retorna quantas moveram (0 = outro request já moveu).

        `stack_id`: quando informado, escopa o move só à chave daquela stack
        (chave pós-migration 0019) — sem isso, moveria TODAS as chaves ativas
        da conta naquela máquina, inclusive de outras stacks/planos que não
        deveriam se mexer. Chaves legadas (sem stack_id gravado) continuam
        usando o comportamento antigo (sem esse filtro)."""
        params = {
            "account_id": f"eq.{account_id}",
            "machine_id": f"eq.{from_machine_id}",
            "status": "eq.active",
        }
        if stack_id:
            params["stack_id"] = f"eq.{stack_id}"
        r = await self._rest.patch(
            "/api_keys",
            params=params,
            json={"machine_id": to_machine_id},
            headers={"Prefer": "return=representation"},
        )
        r.raise_for_status()
        return len(r.json())

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

    async def list_stopped_machines_for_plan(self, plan: str) -> list[dict]:
        """Máquinas pausadas (stopPod) cujo template serve o plano — candidatas
        a auto-wake quando chega request e não há capacidade running. O proxy
        URL do RunPod não muda entre stop/start, então public_url segue válido."""
        r = await self._rest.get(
            "/machines",
            params={
                "status": "eq.stopped",
                "public_url": "not.is.null",
                "runpod_pod_id": "not.is.null",
                "select": "*,templates!inner(plan)",
                "templates.plan": f"eq.{plan}",
                "order": "created_at.asc",
            },
        )
        r.raise_for_status()
        return r.json()

    async def list_machines_with_pod(self) -> list[dict]:
        """Máquinas não-terminadas com pod associado — alvo da reconciliação
        de status do lifecycle (espelho do reconcileMachineStatuses do painel)."""
        r = await self._rest.get(
            "/machines",
            params={
                "status": "in.(creating,running,stopped)",
                "runpod_pod_id": "not.is.null",
                "select": "id,status,runpod_pod_id",
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

    # ---------- stacks ----------

    async def get_stack(self, stack_id: str) -> dict | None:
        r = await self._rest.get(
            "/stacks",
            params={
                "id": f"eq.{stack_id}",
                "select": "id,account_id,machine_id,plan,slug",
                "limit": "1",
            },
        )
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None

    async def repoint_stack(
        self, stack_id: str, from_machine_id: str, to_machine_id: str
    ) -> bool:
        """Reponta a stack pra outra máquina, condicionado à origem esperada
        (UPDATE ... WHERE machine_id = origem) — 0 linhas alteradas significa
        que outro request realocou primeiro; o chamador re-lê e segue."""
        r = await self._rest.patch(
            "/stacks",
            params={
                "id": f"eq.{stack_id}",
                "machine_id": f"eq.{from_machine_id}",
            },
            json={"machine_id": to_machine_id},
            headers={"Prefer": "return=representation"},
        )
        r.raise_for_status()
        return len(r.json()) > 0

    async def count_stacks_on_machine(self, machine_id: str) -> int:
        """Stacks hospedadas na máquina — ocupação padrão do modelo base
        ("1 stack = 1 slot", mesmo critério do machineStackCapacity do painel)."""
        r = await self._rest.get(
            "/stacks",
            params={"machine_id": f"eq.{machine_id}", "select": "id"},
            headers={"Prefer": "count=exact"},
        )
        r.raise_for_status()
        content_range = r.headers.get("content-range", "/0")
        return int(content_range.split("/")[-1])

    async def machine_stack_slots(self, machine_id: str) -> int | None:
        """Slots de stacks da máquina (função SQL única, compartilhada com o
        painel — migration 0018). 0 = capacidade desconhecida/sem teto, mesmo
        contrato do computeCapacity com VRAM nula."""
        r = await self._rest.post(
            "/rpc/machine_stack_slots", json={"p_machine_id": machine_id}
        )
        r.raise_for_status()
        return r.json()

    # ---------- templates ----------

    async def list_distinct_plans(self) -> list[str]:
        """Planos com pelo menos 1 template cadastrado — base do loop de
        reposição proativa (ensure_capacity_once), que roda por plano.
        PostgREST não tem DISTINCT para coluna arbitrária sem RPC; a tabela é
        pequena, então dedup em memória é barato mesmo a cada tick de 300s."""
        r = await self._rest.get("/templates", params={"select": "plan"})
        r.raise_for_status()
        return sorted({row["plan"] for row in r.json()})

    # ---------- system_settings ----------

    async def get_setting(self, key: str, default: bool) -> bool:
        """Flag booleana global (ex.: auto_provision_enabled). Chamador é
        responsável por cachear — aqui é sempre uma leitura live."""
        r = await self._rest.get(
            "/system_settings",
            params={"key": f"eq.{key}", "select": "value", "limit": "1"},
        )
        r.raise_for_status()
        rows = r.json()
        return rows[0]["value"] if rows else default

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
        self,
        account_id: str,
        stack_id: str | None,
        query_embedding: list[float],
        top_k: int = 4,
    ) -> list[str]:
        """Top-k chunks da base de conhecimento da stack por similaridade de
        cosseno (RPC match_knowledge_chunks, mesma função usada pelo painel).
        `stack_id=None` (chave legada sem stack resolvível) mantém o
        comportamento antigo de buscar por toda a conta."""
        r = await self._rest.post(
            "/rpc/match_knowledge_chunks",
            json={
                "p_account_id": account_id,
                "p_stack_id": stack_id,
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
