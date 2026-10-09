"""Read-only client for the orchestrator's incident API (HTTP GET only)."""

import httpx2

from orchestrator.adapters.base import AdapterError
from orchestrator.models import Incident


class OrchestratorIncidentSource:
    def __init__(self, base_url: str, timeout: float = 5.0, transport=None) -> None:
        self._client = httpx2.Client(base_url=base_url, timeout=timeout, transport=transport)

    def _get(self, path: str) -> httpx2.Response:
        try:
            return self._client.get(path)
        except httpx2.HTTPError as exc:
            raise AdapterError(f"Orchestrator unreachable: {exc}") from exc

    def list(self) -> list[Incident]:
        resp = self._get("/incidents")
        try:
            resp.raise_for_status()
            return [Incident.model_validate(item) for item in resp.json()]
        except (httpx2.HTTPError, ValueError) as exc:
            raise AdapterError(f"Orchestrator returned something unusable: {exc}") from exc

    def get(self, incident_id: int) -> Incident | None:
        resp = self._get(f"/incidents/{incident_id}")
        if resp.status_code == 404:
            return None
        try:
            resp.raise_for_status()
            return Incident.model_validate(resp.json())
        except (httpx2.HTTPError, ValueError) as exc:
            raise AdapterError(f"Orchestrator returned something unusable: {exc}") from exc
