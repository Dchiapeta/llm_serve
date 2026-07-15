"""
Cliente mínimo da API REST do RunPod para o gateway (espelho de lib/runpod.ts).

Só o necessário para o lifecycle de máquinas: stop_pod (auto-pausa por
ociosidade) e start_pod (auto-wake quando chega request sem capacidade).
"""

import httpx


class RunPodClient:
    def __init__(self, api_key: str):
        self._client = httpx.AsyncClient(
            base_url="https://rest.runpod.io/v1",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(30.0, connect=5.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def stop_pod(self, pod_id: str) -> dict:
        r = await self._client.post(f"/pods/{pod_id}/stop")
        if r.status_code >= 400:
            raise RuntimeError(f"RunPod POST /pods/{pod_id}/stop → {r.status_code}: {r.text}")
        return r.json() if r.content else {}

    async def list_pods(self) -> list[dict]:
        r = await self._client.get("/pods")
        if r.status_code >= 400:
            raise RuntimeError(f"RunPod GET /pods → {r.status_code}: {r.text}")
        return r.json() or []

    async def start_pod(self, pod_id: str) -> dict:
        # Pode falhar com "not enough free GPUs": pod pausado não reserva GPU
        # e o host pode tê-la cedido (mesmo caso do startMachine do painel)
        r = await self._client.post(f"/pods/{pod_id}/start")
        if r.status_code >= 400:
            raise RuntimeError(f"RunPod POST /pods/{pod_id}/start → {r.status_code}: {r.text}")
        return r.json() if r.content else {}
