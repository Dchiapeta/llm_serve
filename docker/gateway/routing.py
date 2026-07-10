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
        return rows[0]

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
        await self.set_client_location(
            account_id, machine_id=None, lora_status="unloaded"
        )

    async def touch(self, account_id: str) -> None:
        """Marca uso recente (o chamador é responsável pelo throttling)."""
        r = await self._client.post(
            "/rpc/touch_route", json={"p_account_id": account_id}
        )
        r.raise_for_status()

    async def list_routes_by_machine(self, machine_id: str) -> list[dict]:
        r = await self._client.get(
            "/routing_state",
            params={"machine_id": f"eq.{machine_id}", "select": "*"},
        )
        r.raise_for_status()
        return r.json()
