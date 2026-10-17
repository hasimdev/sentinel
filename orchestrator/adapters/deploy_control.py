"""Deploy control adapter: runs a deploy (or rollback) through Harness (see CLAUDE.md adapters).

The AI never uses this. The orchestrator starts a rollback only after a CAB approver approved
the emergency change in ServiceNow. The API key can only run this project's pipelines.
"""

import json
import re
from typing import Literal, Protocol

import httpx2
from pydantic import BaseModel

from orchestrator.adapters.base import AdapterError

RunStatus = Literal["running", "succeeded", "failed"]
VERSION = re.compile(r"^\d+\.\d+\.\d+$")

# Harness execution statuses that mean "finished". Anything else is still going.
SUCCEEDED = {"success", "ignorefailed"}
FAILED = {"failed", "aborted", "expired", "errored", "approvalrejected", "aborted_by_freeze"}


class DeployRun(BaseModel):
    run_id: str
    url: str


class DeployControl(Protocol):
    def start(self, version: str, reason: str) -> DeployRun: ...
    def status(self, run: DeployRun) -> RunStatus: ...


class HarnessDeployControl:
    """DeployControl backed by a Harness pipeline with `version` and `reason` inputs."""

    def __init__(
        self,
        account_id: str,
        api_key: str,
        org: str = "default",
        project: str = "sentinel",
        pipeline: str = "shoplite_deploy",
        api_url: str = "https://app.harness.io/gateway",
        app_url: str = "https://app.harness.io",
        transport=None,
    ) -> None:
        self.account_id, self.org, self.project, self.pipeline = account_id, org, project, pipeline
        self.app_url = app_url.rstrip("/")
        self._scope = {
            "accountIdentifier": account_id,
            "orgIdentifier": org,
            "projectIdentifier": project,
        }
        self._client = httpx2.Client(
            base_url=api_url.rstrip("/"),
            headers={"x-api-key": api_key, "Accept": "application/json"},
            timeout=20,
            transport=transport,
        )

    def start(self, version: str, reason: str) -> DeployRun:
        if not VERSION.fullmatch(version):
            raise AdapterError(f"Not a version: {version!r}")
        inputs = {
            "pipeline": {
                "identifier": self.pipeline,
                "variables": [
                    {"name": "version", "type": "String", "value": version},
                    {"name": "reason", "type": "String", "value": reason[:200]},
                ],
            }
        }
        data = self._call(
            "POST",
            f"/pipeline/api/pipeline/execute/{self.pipeline}",
            params={**self._scope, "moduleType": "cd"},
            content=json.dumps(inputs),  # JSON is valid YAML; Harness reads runtime inputs as YAML
            headers={"Content-Type": "application/yaml"},
        )
        run_id = data["planExecution"]["uuid"]
        return DeployRun(run_id=run_id, url=self._run_url(run_id))

    def status(self, run: DeployRun) -> RunStatus:
        data = self._call(
            "GET", f"/pipeline/api/pipelines/execution/v2/{run.run_id}", params=self._scope
        )
        state = data["pipelineExecutionSummary"]["status"].lower()
        if state in SUCCEEDED:
            return "succeeded"
        if state in FAILED:
            return "failed"
        return "running"

    def _run_url(self, run_id: str) -> str:
        return (
            f"{self.app_url}/ng/account/{self.account_id}/all/orgs/{self.org}"
            f"/projects/{self.project}/pipelines/{self.pipeline}/executions/{run_id}/pipeline"
        )

    def _call(self, method: str, path: str, **kwargs) -> dict:
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx2.HTTPError as exc:
            raise AdapterError(f"Harness unreachable: {exc}") from exc
        if resp.is_error:
            raise AdapterError(_harness_error(resp))
        return resp.json()["data"]


def _harness_error(resp) -> str:
    try:
        detail = resp.json().get("message") or ""
    except ValueError:
        detail = ""
    return f"Harness refused ({resp.status_code}): {(detail or resp.reason_phrase)[:300]}"
