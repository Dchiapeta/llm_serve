"""
Lifecycle dos adapters LoRA: unload por ociosidade e migração ativa.

Roda dentro do gateway (réplica única — ver README) porque só o gateway
conhece os requests in-flight: nunca descarrega/migra com stream em voo.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("gateway.lifecycle")


def lora_name(account_id: str) -> str:
    return f"acct-{account_id}"


class LifecycleManager:
    def __init__(
        self,
        store,
        supa,
        call_agent,
        in_flight: dict,
        idle_unload_minutes: float,
        drain_timeout_s: float,
        lora_load_timeout_s: float,
    ):
        self.store = store
        self.supa = supa
        self.call_agent = call_agent
        self.in_flight = in_flight
        self.idle_unload_minutes = idle_unload_minutes
        self.drain_timeout_s = drain_timeout_s
        self.lora_load_timeout_s = lora_load_timeout_s

    def _in_flight_count(self, account_id: str, machine_id: str) -> int:
        return self.in_flight.get((account_id, machine_id), 0)

    # ---------- Idle reaper ----------

    async def reap_idle_once(self) -> list[str]:
        """Descarrega adapters ociosos além do limite. Retorna as contas tratadas."""
        if self.idle_unload_minutes <= 0:
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=self.idle_unload_minutes)
        routes = await self.store.list_idle_routes(cutoff.isoformat())
        reaped: list[str] = []
        for route in routes:
            account_id, machine_id = route["account_id"], route["machine_id"]
            if not machine_id:
                continue
            # nunca descarrega com request em voo — fica pro próximo ciclo
            if self._in_flight_count(account_id, machine_id) > 0:
                continue
            machine = await self.supa.get_machine(machine_id)
            if not machine:
                await self.store.mark_slot_idle(account_id)
                continue
            try:
                # unload primeiro, slot liberado depois: se o unload falhar a
                # rota continua 'loaded' e o próximo ciclo tenta de novo
                await self.call_agent(
                    machine, "/unload-lora", {"lora_name": lora_name(account_id)}
                )
                await self.store.mark_slot_idle(account_id)
                reaped.append(account_id)
                logger.info("idle reaper: adapter de %s descarregado de %s", account_id, machine_id)
            except Exception as e:
                logger.warning("idle reaper: unload de %s falhou (%s) — mantém loaded", account_id, e)
        return reaped

    async def idle_reaper_loop(self, interval_s: float = 60.0):
        while True:
            await asyncio.sleep(interval_s)
            try:
                await self.reap_idle_once()
            except Exception as e:
                logger.warning("idle reaper: ciclo falhou (%s)", e)

    # ---------- Migração ativa ----------

    async def migrate(self, account_id: str, target_machine_id: str) -> dict:
        """Migra o adapter de uma conta para outra máquina, sem perder request.

        Sequência: marca 'migrating' (origem continua servindo pelo machine_id),
        carrega no destino, flip atômico do machine_id, drena os requests em
        voo na origem e só então descarrega a origem.
        """
        route = await self.store.get_client_location(account_id)
        if not route or route["lora_status"] != "loaded" or not route["machine_id"]:
            raise MigrationError(409, f"conta sem rota 'loaded' para migrar (estado: {route and route['lora_status']})")
        origin_id = route["machine_id"]
        if origin_id == target_machine_id:
            raise MigrationError(400, "máquina de destino é a própria origem")

        target = await self.supa.get_machine(target_machine_id)
        if not target or target.get("status") != "running" or not target.get("public_url"):
            raise MigrationError(400, "máquina de destino não está running")
        origin = await self.supa.get_machine(origin_id)

        adapter = await self.supa.latest_ready_adapter(account_id)
        if not adapter:
            raise MigrationError(409, "conta não tem adapter ready registrado")

        # 1. migrating: bloqueia reaper e novos claims; requests novos seguem
        #    indo à origem (o roteamento usa machine_id, que ainda é a origem)
        await self.store.set_client_location(account_id, lora_status="migrating")

        # 2. chaves + load no destino; falha → volta 'loaded' na origem
        try:
            keys = await self.supa.list_active_keys_for_account(account_id)
            if keys:
                await self.call_agent(target, "/upsert-keys", {"keys": keys})
            files = await self.supa.signed_lora_files(adapter["storage_path"])
            await self.call_agent(
                target, "/load-lora",
                {"lora_name": lora_name(account_id), "files": files},
                timeout_s=self.lora_load_timeout_s,
            )
        except MigrationError:
            await self.store.set_client_location(account_id, lora_status="loaded")
            raise
        except Exception as e:
            await self.store.set_client_location(account_id, lora_status="loaded")
            raise MigrationError(502, f"load no destino falhou: {e}")

        # 3. flip: só após o load confirmado — requests novos vão pro destino
        await self.store.set_client_location(
            account_id,
            machine_id=target_machine_id,
            lora_adapter_id=adapter["id"],
            lora_status="loaded",
        )

        # 4. drain: espera os requests em voo NA ORIGEM terminarem —
        #    nunca corta um stream no meio
        drain_deadline = time.time() + self.drain_timeout_s
        drained = True
        while self._in_flight_count(account_id, origin_id) > 0:
            if time.time() > drain_deadline:
                drained = False
                logger.warning(
                    "migração de %s: drain excedeu %ss com %d request(s) em voo — unload da origem adiado",
                    account_id, self.drain_timeout_s,
                    self._in_flight_count(account_id, origin_id),
                )
                break
            await asyncio.sleep(0.5)

        # 5. unload na origem (best-effort: a rota já aponta pro destino;
        #    se falhar, o idle reaper não volta aqui — fica órfão até o pod
        #    reiniciar — então logamos alto)
        unloaded = False
        if drained and origin:
            try:
                await self.call_agent(
                    origin, "/unload-lora", {"lora_name": lora_name(account_id)}
                )
                unloaded = True
            except Exception as e:
                logger.warning("migração de %s: unload da origem falhou (%s)", account_id, e)

        return {
            "ok": True,
            "account_id": account_id,
            "from": origin_id,
            "to": target_machine_id,
            "drained": drained,
            "origin_unloaded": unloaded,
        }


class MigrationError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)
