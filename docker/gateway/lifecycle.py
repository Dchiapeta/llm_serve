"""
Lifecycle dos adapters LoRA e das máquinas: unload por ociosidade, migração
ativa, consolidação de máquinas quase vazias, reconciliação de status com o
RunPod e auto-pausa de pods ociosos.

Roda dentro do gateway (réplica única — ver README) porque só o gateway
conhece os requests in-flight: nunca descarrega/migra/pausa com stream em voo.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("gateway.lifecycle")


def lora_name(stack_id: str) -> str:
    # adapter escopado por STACK (migration 0029); prefixo "acct-" mantido por
    # compat com os filtros de /v1/models. Espelha main.py:lora_name.
    return f"acct-{stack_id}"


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
        machine_free_slots=None,
        runpod=None,
        machine_idle_stop_minutes: float = 0.0,
        consolidation_max_origin_routes: int = 2,
        stop_recheck_grace_s: float = 5.0,
        try_provision_for_pool=None,
        pool_watermark_slots: float = 5.0,
        auto_provision_enabled=None,
        on_machine_running=None,
    ):
        self.store = store
        self.supa = supa
        self.call_agent = call_agent
        self.in_flight = in_flight
        self.idle_unload_minutes = idle_unload_minutes
        self.drain_timeout_s = drain_timeout_s
        self.lora_load_timeout_s = lora_load_timeout_s
        # capacidade injetada por main.py (mesma conta do pick_machine_with_free_slot)
        self.machine_free_slots = machine_free_slots
        self.runpod = runpod
        self.machine_idle_stop_minutes = machine_idle_stop_minutes
        self.consolidation_max_origin_routes = consolidation_max_origin_routes
        self.stop_recheck_grace_s = stop_recheck_grace_s
        # reposição proativa (ensure_capacity_once) — callbacks injetados por
        # main.py, mesmo padrão de machine_free_slots acima
        self.try_provision_for_pool = try_provision_for_pool
        self.pool_watermark_slots = pool_watermark_slots
        self.auto_provision_enabled = auto_provision_enabled
        # callback síncrono chamado quando a reconciliação observa uma máquina
        # promovida a running (ex.: religada pelo console do RunPod) — main.py
        # usa pra agendar o reenvio de chaves ao agent, que reinicia zerado
        self.on_machine_running = on_machine_running
        # unloads adiados: quando o drain de uma migração estoura o timeout, a
        # origem fica com o adapter carregado (órfão) ocupando slot. Em vez de
        # esperar o pod reiniciar, guardamos (origin_id, account_id) aqui e o
        # lifecycle loop reprocessa a cada ciclo (process_pending_unloads_once),
        # descarregando assim que o in-flight na origem zerar. In-memory (mesma
        # limitação de réplica única do resto do estado — ver finding #13).
        self.pending_unloads: list[dict] = []

    def _in_flight_count(self, stack_id: str, machine_id: str) -> int:
        # in_flight é chaveado por (stack_id, machine_id) desde a migration 0029
        return self.in_flight.get((stack_id, machine_id), 0)

    def _machine_in_flight(self, machine_id: str) -> int:
        return sum(n for (_, m), n in self.in_flight.items() if m == machine_id and n > 0)

    def _enqueue_pending_unload(self, machine_id: str, stack_id: str) -> None:
        if any(
            p["machine_id"] == machine_id and p["stack_id"] == stack_id
            for p in self.pending_unloads
        ):
            return
        self.pending_unloads.append({"machine_id": machine_id, "stack_id": stack_id})

    async def process_pending_unloads_once(self) -> list[str]:
        """Reprocessa os unloads adiados por drain timeout (ver migrate step 4).
        Descarrega a origem assim que o in-flight zera; se a stack voltou a ser
        roteada pra essa mesma máquina, cancela o unload (o adapter está em uso).
        Retorna as stacks efetivamente descarregadas."""
        if not self.pending_unloads:
            return []
        done: list[str] = []
        still_pending: list[dict] = []
        for p in self.pending_unloads:
            machine_id, stack_id = p["machine_id"], p["stack_id"]
            # a stack voltou pra origem no meio-tempo → adapter em uso, cancela
            route = await self.store.get_client_location(stack_id)
            if route and route.get("machine_id") == machine_id:
                logger.info(
                    "unload adiado da stack %s cancelado: voltou a rotear pra %s",
                    stack_id, machine_id,
                )
                continue
            # ainda com request em voo na origem → tenta no próximo ciclo
            if self._in_flight_count(stack_id, machine_id) > 0:
                still_pending.append(p)
                continue
            machine = await self.supa.get_machine(machine_id)
            if not machine:
                # máquina não existe mais (terminada) → nada a descarregar
                continue
            try:
                await self.call_agent(
                    machine, "/unload-lora", {"lora_name": lora_name(stack_id)}
                )
                done.append(stack_id)
                logger.info(
                    "unload adiado da stack %s concluído em %s", stack_id, machine_id
                )
            except Exception as e:
                logger.warning(
                    "unload adiado da stack %s falhou (%s) — tenta no próximo ciclo",
                    stack_id, e,
                )
                still_pending.append(p)
        self.pending_unloads = still_pending
        return done

    # ---------- Idle reaper ----------

    async def reap_idle_once(self) -> list[str]:
        """Descarrega adapters ociosos além do limite. Retorna as contas tratadas."""
        if self.idle_unload_minutes <= 0:
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=self.idle_unload_minutes)
        routes = await self.store.list_idle_routes(cutoff.isoformat())
        reaped: list[str] = []
        for route in routes:
            stack_id, machine_id = route["stack_id"], route["machine_id"]
            if not machine_id:
                continue
            # nunca descarrega com request em voo — fica pro próximo ciclo
            if self._in_flight_count(stack_id, machine_id) > 0:
                continue
            machine = await self.supa.get_machine(machine_id)
            if not machine:
                await self.store.mark_slot_idle(stack_id)
                continue
            try:
                # unload primeiro, slot liberado depois: se o unload falhar a
                # rota continua 'loaded' e o próximo ciclo tenta de novo
                await self.call_agent(
                    machine, "/unload-lora", {"lora_name": lora_name(stack_id)}
                )
                await self.store.mark_slot_idle(stack_id)
                reaped.append(stack_id)
                logger.info("idle reaper: adapter da stack %s descarregado de %s", stack_id, machine_id)
            except Exception as e:
                logger.warning("idle reaper: unload da stack %s falhou (%s) — mantém loaded", stack_id, e)
        return reaped

    async def reap_idle_base_stacks_once(self) -> list[str]:
        """Libera a "casa" de stacks de MODELO BASE ociosas (stacks.machine_id
        → NULL), liberando a vaga ponderada da máquina pros demais. Ao contrário
        do reap_idle_once (LoRA), NÃO chama o agent: o modelo base segue
        carregado pros co-tenants, liberar é só contábil. Na próxima request a
        stack é re-alocada por place_base_stack (main.py). Retorna as stacks
        liberadas."""
        if self.idle_unload_minutes <= 0:
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=self.idle_unload_minutes)
        stacks = await self.supa.list_idle_base_stacks(cutoff.isoformat())
        released: list[str] = []
        for s in stacks:
            stack_id, machine_id = s["id"], s["machine_id"]
            if not machine_id:
                continue
            # nunca libera com request em voo (inclui streams longos) — fica pro
            # próximo ciclo; in_flight é chaveado por (stack_id, machine_id)
            if self._in_flight_count(stack_id, machine_id) > 0:
                continue
            try:
                # condicional na origem: 0 linhas = request concorrente já
                # re-homeou/moveu a stack, então nada a fazer
                if await self.supa.release_base_stack(stack_id, machine_id):
                    released.append(stack_id)
                    logger.info(
                        "idle reaper: stack base %s liberada de %s", stack_id, machine_id
                    )
                    try:
                        await self.supa.log_machine_event(
                            machine_id, "stack_released",
                            f"Stack {stack_id} liberada por ociosidade (vaga base livre)",
                        )
                    except Exception:
                        pass  # histórico é best-effort
            except Exception as e:
                logger.warning(
                    "idle reaper: liberar stack base %s falhou (%s)", stack_id, e
                )
        return released

    async def idle_reaper_loop(self, interval_s: float = 60.0):
        while True:
            await asyncio.sleep(interval_s)
            try:
                await self.reap_idle_once()
            except Exception as e:
                logger.warning("idle reaper: ciclo falhou (%s)", e)
            try:
                await self.reap_idle_base_stacks_once()
            except Exception as e:
                logger.warning("idle reaper (base): ciclo falhou (%s)", e)

    # ---------- Migração ativa ----------

    async def migrate(self, stack_id: str, target_machine_id: str) -> dict:
        """Migra o adapter de uma STACK para outra máquina, sem perder request.

        Sequência: marca 'migrating' (origem continua servindo pelo machine_id),
        carrega no destino, flip atômico do machine_id, drena os requests em
        voo na origem e só então descarrega a origem.
        """
        route = await self.store.get_client_location(stack_id)
        if not route or route["lora_status"] != "loaded" or not route["machine_id"]:
            raise MigrationError(409, f"stack sem rota 'loaded' para migrar (estado: {route and route['lora_status']})")
        account_id = route["account_id"]  # denormalizado na rota, p/ chaves/histórico
        origin_id = route["machine_id"]
        if origin_id == target_machine_id:
            raise MigrationError(400, "máquina de destino é a própria origem")

        target = await self.supa.get_machine(target_machine_id)
        if not target or target.get("status") != "running" or not target.get("public_url"):
            raise MigrationError(400, "máquina de destino não está running")
        origin = await self.supa.get_machine(origin_id)

        adapter = await self.supa.latest_ready_adapter_for_stack(stack_id)
        if not adapter:
            raise MigrationError(409, "stack não tem adapter ready registrado")

        # 1. migrating: bloqueia reaper e novos claims; requests novos seguem
        #    indo à origem (o roteamento usa machine_id, que ainda é a origem)
        await self.store.set_client_location(stack_id, lora_status="migrating")

        # 2. chaves + load no destino; falha → volta 'loaded' na origem. As
        #    chaves são por conta (api_keys.account_id); upsertar todas na origem
        #    é superset seguro — inclui a da stack que está migrando.
        try:
            keys = await self.supa.list_active_keys_for_account(account_id)
            if keys:
                await self.call_agent(target, "/upsert-keys", {"keys": keys})
            files = await self.supa.signed_lora_files(adapter["storage_path"])
            await self.call_agent(
                target, "/load-lora",
                {"lora_name": lora_name(stack_id), "files": files},
                timeout_s=self.lora_load_timeout_s,
            )
        except MigrationError:
            await self.store.set_client_location(stack_id, lora_status="loaded")
            raise
        except Exception as e:
            await self.store.set_client_location(stack_id, lora_status="loaded")
            raise MigrationError(502, f"load no destino falhou: {e}")

        # 3. flip: só após o load confirmado — requests novos vão pro destino
        await self.store.set_client_location(
            stack_id,
            machine_id=target_machine_id,
            lora_adapter_id=adapter["id"],
            lora_status="loaded",
        )

        # 4. drain: espera os requests em voo NA ORIGEM terminarem —
        #    nunca corta um stream no meio
        drain_deadline = time.time() + self.drain_timeout_s
        drained = True
        while self._in_flight_count(stack_id, origin_id) > 0:
            if time.time() > drain_deadline:
                drained = False
                logger.warning(
                    "migração da stack %s: drain excedeu %ss com %d request(s) em voo — unload da origem adiado",
                    stack_id, self.drain_timeout_s,
                    self._in_flight_count(stack_id, origin_id),
                )
                # enfileira o unload adiado: o lifecycle loop reprocessa quando
                # o in-flight zerar, em vez de deixar o adapter órfão até restart
                self._enqueue_pending_unload(origin_id, stack_id)
                break
            await asyncio.sleep(0.5)

        # 5. unload na origem (best-effort: a rota já aponta pro destino;
        #    se falhar, o idle reaper não volta aqui — fica órfão até o pod
        #    reiniciar — então logamos alto)
        unloaded = False
        if drained and origin:
            try:
                await self.call_agent(
                    origin, "/unload-lora", {"lora_name": lora_name(stack_id)}
                )
                unloaded = True
            except Exception as e:
                logger.warning("migração da stack %s: unload da origem falhou (%s)", stack_id, e)

        return {
            "ok": True,
            "stack_id": stack_id,
            "account_id": account_id,
            "from": origin_id,
            "to": target_machine_id,
            "drained": drained,
            "origin_unloaded": unloaded,
        }


    # ---------- Consolidação de máquinas ----------

    async def consolidate_once(self) -> list[dict]:
        """Esvazia uma máquina quase vazia migrando suas contas para a máquina
        mais cheia (mesmo template) que ainda caiba todas. Ex.: A=16, B=1 →
        A=17, B=0. A origem esvaziada pausa depois via stop_idle_machines_once.

        No máximo 1 máquina-origem por ciclo — mantém o sistema calmo.
        """
        if self.machine_free_slots is None:
            return []
        machines = await self.supa.list_running_machines()

        by_template: dict[str, list[dict]] = {}
        for m in machines:
            if m.get("template_id"):
                by_template.setdefault(m["template_id"], []).append(m)

        for group in by_template.values():
            if len(group) < 2:
                continue
            counts = {m["id"]: await self.supa.count_active_routes(m["id"]) for m in group}

            # origem: a MENOS cheia, com 1..N rotas — candidata a esvaziar
            origins = sorted(
                (m for m in group if 1 <= counts[m["id"]] <= self.consolidation_max_origin_routes),
                key=lambda m: counts[m["id"]],
            )
            for origin in origins:
                routes = await self.store.list_routes_by_machine(origin["id"])
                # estado transitório (loading/migrating) ou stream em voo →
                # decide no próximo ciclo, nunca força
                if any(r["lora_status"] != "loaded" for r in routes):
                    continue
                if any(self._in_flight_count(r["stack_id"], origin["id"]) > 0 for r in routes):
                    continue

                # destino: a MAIS cheia que ainda caiba TODAS as rotas da origem
                target = None
                for m in sorted(group, key=lambda m: counts[m["id"]], reverse=True):
                    if m["id"] == origin["id"]:
                        continue
                    if await self.machine_free_slots(m) >= len(routes):
                        target = m
                        break
                if not target:
                    continue

                moved: list[dict] = []
                for route in routes:
                    try:
                        result = await self.migrate(route["stack_id"], target["id"])
                        moved.append(result)
                    except Exception as e:
                        logger.warning(
                            "consolidação: migração da stack %s (%s → %s) falhou (%s) — interrompe a origem",
                            route["stack_id"], origin["id"], target["id"], e,
                        )
                        break
                if moved:
                    try:
                        await self.supa.log_machine_event(
                            origin["id"], "sync",
                            f"Consolidação: {len(moved)} conta(s) migrada(s) para {target.get('name') or target['id']}",
                        )
                    except Exception:
                        pass
                    logger.info(
                        "consolidação: %d conta(s) migrada(s) de %s para %s",
                        len(moved), origin["id"], target["id"],
                    )
                return moved
        return []

    # ---------- Reconciliação de status com o RunPod ----------

    # Espelho do POD_STATUS_MAP de lib/machines.ts
    POD_STATUS_MAP = {"RUNNING": "running", "EXITED": "stopped", "TERMINATED": "terminated"}

    async def reconcile_statuses_once(self) -> list[tuple[str, str]]:
        """Alinha machines.status com o estado real dos pods no RunPod.

        O painel só reconcilia quando alguém abre uma página; sem isso, uma
        máquina recém-criada fica 'creating' no banco para sempre — invisível
        para a auto-pausa (que filtra status=running) mesmo com o pod cobrando
        GPU. Aqui o gateway reconcilia sozinho a cada ciclo do lifecycle.
        """
        if self.runpod is None:
            return []
        machines = await self.supa.list_machines_with_pod()
        if not machines:
            return []
        try:
            pods = await self.runpod.list_pods()
        except Exception as e:
            logger.warning("reconcile: listagem de pods falhou (%s) — mantém o banco", e)
            return []
        pod_by_id = {p["id"]: p for p in pods}

        changed: list[tuple[str, str]] = []
        for m in machines:
            pod = pod_by_id.get(m["runpod_pod_id"])
            if pod is None:
                # pod sumiu da API → terminada; em 'creating' pode só não ter
                # aparecido ainda (mesma cautela do painel) — não marca
                if m["status"] == "creating":
                    continue
                new_status = "terminated"
            else:
                new_status = self.POD_STATUS_MAP.get(pod.get("desiredStatus"), m["status"])
            if new_status == m["status"]:
                continue
            if new_status == "running":
                # o relógio de ociosidade zera em QUALQUER promoção a running
                # (creating→running ou stopped→running via console do RunPod):
                # sem isso, uma máquina religada com last_activity_at velho é
                # auto-pausada no ciclo seguinte, antes de servir qualquer request
                try:
                    await self.supa.touch_machine_activity(m["id"])
                except Exception:
                    pass
                if self.on_machine_running:
                    try:
                        self.on_machine_running(m["id"])
                    except Exception:
                        pass
            # compare-and-set: o new_status foi decidido a partir de m["status"]
            # (lido no topo do loop) + o snapshot do RunPod. Se o painel ou outro
            # ciclo mudou o status nesse meio-tempo, não sobrescreve — reconcilia
            # no próximo ciclo com o valor fresco. Fecha a corrida do finding #7
            # (ex.: painel marca 'stopped' e o reconcile, com snapshot velho,
            # devolvia pra 'running').
            applied = await self.supa.set_machine_status(
                m["id"], new_status, expected=m["status"]
            )
            if not applied:
                logger.info(
                    "reconcile: status de %s mudou concorrentemente (esperava %s) — pula",
                    m["id"], m["status"],
                )
                continue
            changed.append((m["id"], new_status))
            logger.info("reconcile: máquina %s %s → %s", m["id"], m["status"], new_status)
        return changed

    # ---------- Auto-pausa de máquinas ociosas ----------

    async def stop_idle_machines_once(self) -> list[str]:
        """Pausa (stopPod) máquinas running sem nenhuma atividade há
        machine_idle_stop_minutes e sem rotas ativas. Religar acontece pelo
        auto-wake do gateway (request sem capacidade) ou manualmente no painel.
        """
        if self.runpod is None or self.machine_idle_stop_minutes <= 0:
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=self.machine_idle_stop_minutes)
        stopped: list[str] = []
        for m in await self.supa.list_running_machines():
            machine_id, pod_id = m["id"], m.get("runpod_pod_id")
            raw = m.get("last_activity_at")
            if not pod_id or not raw:
                continue
            last_activity = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if last_activity >= cutoff:
                continue
            if await self.supa.count_active_routes(machine_id) > 0:
                continue
            if self._machine_in_flight(machine_id) > 0:
                continue

            # flip primeiro: picks filtram status=running, então novos claims
            # param de enxergar a máquina antes do stop de verdade
            await self.supa.set_machine_status(machine_id, "stopped")
            await asyncio.sleep(self.stop_recheck_grace_s)
            if (
                await self.supa.count_active_routes(machine_id) > 0
                or self._machine_in_flight(machine_id) > 0
            ):
                # um claim escapou na janela pick→claim — desiste desta rodada
                await self.supa.set_machine_status(machine_id, "running")
                continue
            try:
                await self.runpod.stop_pod(pod_id)
            except Exception as e:
                await self.supa.set_machine_status(machine_id, "running")
                logger.warning("auto-pausa: stopPod de %s falhou (%s)", machine_id, e)
                continue
            try:
                await self.supa.log_machine_event(
                    machine_id, "stopped",
                    f"Auto-pausa: sem atividade há {self.machine_idle_stop_minutes:g} min",
                )
            except Exception:
                pass
            logger.info("auto-pausa: máquina %s (pod %s) pausada por ociosidade", machine_id, pod_id)
            stopped.append(machine_id)
        return stopped

    # ---------- Reposição proativa de capacidade ----------

    async def ensure_capacity_once(self) -> list[str]:
        """Por plano, soma os slots livres de TODAS as máquinas não-terminadas
        (running + stopped — machine_free_slots é capacidade menos rotas
        ativas, então uma pausada vazia entra com a capacidade cheia, ex.
        20, já que ela é "disponível via despausar"). Abaixo do watermark,
        dispara a criação de 1 máquina nova — que try_provision_for_pool
        pausa assim que ficar saudável, virando a próxima reserva. Regra
        única e autolimitante: assim que existe 1 reserva pausada, a soma já
        fica bem acima do watermark, então não dispara outra criação — sem
        precisar de um teto numérico separado de "quantas máquinas".
        """
        if self.try_provision_for_pool is None or self.machine_free_slots is None:
            return []
        if self.auto_provision_enabled is not None and not await self.auto_provision_enabled():
            return []
        triggered: list[str] = []
        for plan in await self.supa.list_distinct_plans():
            # isolado por plano: uma falha (ex.: RPC do Supabase) não deve
            # abortar os planos seguintes deste mesmo tick
            try:
                running = await self.supa.list_running_machines_for_plan(plan)
                stopped = await self.supa.list_stopped_machines_for_plan(plan)
                free_slots_total = 0
                for m in running + stopped:
                    free_slots_total += await self.machine_free_slots(m)
                if free_slots_total >= self.pool_watermark_slots:
                    continue
                reason = f"reposição proativa (slots livres do plano: {free_slots_total})"
                if await self.try_provision_for_pool(plan, reason):
                    triggered.append(plan)
            except Exception as e:
                logger.warning("ensure-capacity: plano %s falhou (%s)", plan, e)
        return triggered

    async def machine_lifecycle_loop(self, interval_s: float = 300.0):
        while True:
            await asyncio.sleep(interval_s)
            # reconciliação primeiro: consolidação e auto-pausa decidem em cima
            # do status real dos pods, não do que o painel deixou no banco
            try:
                await self.reconcile_statuses_once()
            except Exception as e:
                logger.warning("reconcile: ciclo falhou (%s)", e)
            # limpa adapters órfãos de migrações cujo drain estourou o timeout,
            # antes da consolidação/auto-pausa (libera slot que elas consideram)
            try:
                await self.process_pending_unloads_once()
            except Exception as e:
                logger.warning("unloads adiados: ciclo falhou (%s)", e)
            try:
                await self.consolidate_once()
            except Exception as e:
                logger.warning("consolidação: ciclo falhou (%s)", e)
            try:
                await self.stop_idle_machines_once()
            except Exception as e:
                logger.warning("auto-pausa: ciclo falhou (%s)", e)
            # depois da auto-pausa: uma máquina recém-pausada por ociosidade
            # já conta como a reserva vazia do plano nesta mesma rodada
            try:
                await self.ensure_capacity_once()
            except Exception as e:
                logger.warning("ensure-capacity: ciclo falhou (%s)", e)


class MigrationError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)
