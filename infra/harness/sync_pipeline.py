"""Create or update the shoplite-deploy pipeline in Harness from the YAML in this folder.

Pipeline as code: the file in git is exactly what Harness runs. `make harness-pipeline`.
The orchestrator's key can only *run* pipelines, so this needs a key allowed to create or
edit them: grant that briefly (e.g. Project Member on the service account), run this, then
take it away again.
"""

import sys
from pathlib import Path

import httpx2
from dotenv import dotenv_values

PIPELINE = Path(__file__).with_name("shoplite-deploy.pipeline.yaml")
API = "https://app.harness.io/gateway/pipeline/api/pipelines"


def main() -> None:
    env = dotenv_values(Path(__file__).resolve().parents[2] / ".env")
    key, account = env.get("HARNESS_API_KEY", "").strip(), env.get("HARNESS_ACCOUNT_ID", "")
    if not key or not account:
        sys.exit("Set HARNESS_ACCOUNT_ID and HARNESS_API_KEY in .env first.")
    scope = {
        "accountIdentifier": account,
        "orgIdentifier": "default",
        "projectIdentifier": "sentinel",
    }
    client = httpx2.Client(headers={"x-api-key": key}, timeout=30)
    yaml_text = PIPELINE.read_text(encoding="utf-8")
    headers = {"Content-Type": "application/yaml"}

    exists = client.get(f"{API}/shoplite_deploy", params=scope).status_code == 200
    if exists:
        resp = client.put(
            f"{API}/v2/shoplite_deploy", params=scope, content=yaml_text, headers=headers
        )
    else:
        resp = client.post(f"{API}/v2", params=scope, content=yaml_text, headers=headers)
    if resp.is_error:
        try:
            detail = resp.json().get("message", "")
        except ValueError:
            detail = resp.text[:300]
        sys.exit(f"Harness refused ({resp.status_code}): {detail}")
    print(f"Pipeline shoplite_deploy {'updated' if exists else 'created'} from {PIPELINE.name}")


if __name__ == "__main__":
    main()
