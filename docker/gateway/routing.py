"""
Camada de acesso ao estado de roteamento (tabela routing_state) via PostgREST.

Equivalente Python de lib/routing.ts — mesmas operações, mesma semântica.
Toda leitura/escrita do routing no gateway passa por aqui; a atomicidade do
claim vive nas funções SQL (claim_route/touch_route), não no cliente.
"""

from datetime import datetime, timezone

import httpx


class RoutingStore:
    def __init__(self, supabase_url: str, service_role_key: str):
        self._client = httpx.AsyncClient(
            base_url=f"{supabase_url}/rest/v1",
            headers={
                "apikey": service_role_key,
                "Authorization": f"Bearer {service_role_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(10.0),
        )

    async def aclose(self):
        await self._client.aclose()

    async def get_client_location(self, account_id: str) -> dict | None:
        r = await self._client.get(
            "/routing_state",
            params={"account_id": f"eq.{account_id}", "select": "*"},
        )
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None

    async def claim_client_location(self, account_id: str, machine_id: str) -> dict:
        """Claim atômico via RPC. Retorna o estado com a flag 'claimed'."""
        r = await self._client.post(
            "/rpc/claim_route",
            json={"p_account_id": account_id, "p_machine_id": machine_id},
        )
        r.raise_for_status()
        rows = r.json()
        if not rows:
            raise RuntimeError("claim_route não retornou estado")
        row = rows[0]
        if row.get("claimed"):
            await self._record_routing_history(
                account_id=account_id,
                event="allocated",
                machine_id=row.get("machine_id"),
                lora_adapter_id=row.get("lora_adapter_id"),
            )
        return row

    async def _record_routing_history(
        self,
        *,
        account_id: str,
        event: str,
        machine_id: str | None = None,
        from_machine_id: str | None = None,
        lora_adapter_id: str | None = None,
    ) -> None:
        """Espelho de recordRoutingHistory em lib/routing.ts — mesma tabela,
        mesma semântica de eventos (allocated/migrated/released)."""
        r = await self._client.post(
            "/routing_history",
            json={
                "account_id": account_id,
                "event": event,
                "machine_id": machine_id,
                "from_machine_id": from_machine_id,
                "lora_adapter_id": lora_adapter_id,
            },
        )
        r.raise_for_status()

    async def record_reallocation(
        self, account_id: str, *, from_machine_id: str, machine_id: str
    ) -> None:
        """Registra a realocação automática de stack (máquina pausada → nova)
        no histórico — mesmo evento 'migrated' das migrações de adapter."""
        await self._record_routing_history(
            account_id=account_id,
            event="migrated",
            machine_id=machine_id,
            from_machine_id=from_machine_id,
        )

    async def set_client_location(self, account_id: str, **patch) -> None:
        """Atualiza machine_id / lora_adapter_id / lora_status da rota."""
        allowed = {"machine_id", "lora_adapter_id", "lora_status"}
        unknown = set(patch) - allowed
        if unknown:
            raise ValueError(f"campos inválidos no patch: {unknown}")
        patch["updated_at"] = datetime.now(timezone.utc).isoformat()
        r = await self._client.patch(
            "/routing_state",
            params={"account_id": f"eq.{account_id}"},
            json=patch,
        )
        r.raise_for_status()

    async def mark_slot_idle(self, account_id: str) -> None:
        """Libera o slot: sem adapter em VRAM e sem máquina — apto a novo claim."""
        previous = await self.get_client_location(account_id)
        await self.set_client_location(
            account_id, machine_id=None, lora_status="unloaded"
        )
        if previous and previous.get("machine_id"):
            await self._record_routing_history(
                account_id=account_id,
                event="released",
                from_machine_id=previous.get("machine_id"),
                lora_adapter_id=previous.get("lora_adapter_id"),
            )

    async def touch(self, account_id: str) -> None:
        """Marca uso recente (o chamador é responsável pelo throttling)."""
        r = await self._client.post(
            "/rpc/touch_route", json={"p_account_id": account_id}
        )
        r.raise_for_status()

    async def list_idle_routes(self, cutoff_iso: str) -> list[dict]:
        """Rotas 'loaded' sem uso desde o cutoff — candidatas a unload.
        Rotas em 'migrating' nunca entram aqui (o status protege do reaper)."""
        r = await self._client.get(
            "/routing_state",
            params={
                "lora_status": "eq.loaded",
                "last_used_at": f"lt.{cutoff_iso}",
                "select": "*",
            },
        )
        r.raise_for_status()
        return r.json()

    async def list_routes_by_machine(self, machine_id: str) -> list[dict]:
        r = await self._client.get(
            "/routing_state",
            params={"machine_id": f"eq.{machine_id}", "select": "*"},
        )
        r.raise_for_status()
        return r.json()

    async def list_stale_transitional_routes(self, cutoff_iso: str) -> list[dict]:
        """Rotas presas em 'loading'/'migrating' há mais tempo do que
        qualquer operação legítima levaria (LORA_LOAD_TIMEOUT_S,
        MIGRATION_DRAIN_TIMEOUT_S) — candidatas a recuperação automática.

        claim_route e set_client_location sempre tocam updated_at na
        transição pra esses estados; nada além do próprio fluxo de
        load/migração tira uma rota deles, então uma exceção no meio do
        caminho (rede pro Supabase falhar bem no update de reversão, por
        exemplo) deixa a linha presa pra sempre sem isso aqui — todo request
        futuro da conta bateria em wait_until_routed e nunca mais sairia de
        503 "adapter carregando"."""
        r = await self._client.get(
            "/routing_state",
            params={
                "lora_status": "in.(loading,migrating)",
                "updated_at": f"lt.{cutoff_iso}",
                "select": "*",
            },
        )
        r.raise_for_status()
        return r.json()
