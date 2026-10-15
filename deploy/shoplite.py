"""Build and deploy ShopLite releases.

Every release is built from a Git tag `shoplite-v<version>`, so a version always means the
same code. Deploying swaps the running container; rolling back is deploying an older version.
Each deploy is recorded in logs/deploys.log (shipped to Loki) with the four standard tags.

  python -m deploy.shoplite release 0.2.0   # build the image from tag shoplite-v0.2.0
  python -m deploy.shoplite deploy 0.2.0    # run that version (replaces the current one)
  python -m deploy.shoplite status          # which version is running
  python -m deploy.shoplite stop
"""

import argparse
import json
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx2

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "sentinel/shoplite"
CONTAINER = "sentinel-shoplite"
PORT = 8000
DEPLOY_LOG = ROOT / "logs" / "deploys.log"

Runner = Callable[[list[str]], str]


class ReleaseError(Exception):
    """A release or deploy could not be done; the message says what to do."""


def run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603 (fixed commands)
    if result.returncode != 0:
        raise ReleaseError(f"{' '.join(cmd[:3])} failed: {result.stderr.strip()[:500]}")
    return result.stdout.strip()


def tag_for(version: str) -> str:
    return f"shoplite-v{version}"


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def commit_of(version: str, runner: Runner = run) -> str:
    try:
        return runner(
            ["git", "-C", str(ROOT), "rev-parse", "--short", f"{tag_for(version)}^{{commit}}"]
        )
    except ReleaseError as exc:
        raise ReleaseError(
            f"No release tag {tag_for(version)}. Tag the commit first: "
            f"git tag {tag_for(version)} <commit>"
        ) from exc


def release(version: str, runner: Runner = run) -> str:
    """Build the image for `version` from its Git tag. Returns the commit it was built from."""
    sha = commit_of(version, runner)
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "source.tar"
        source = Path(tmp) / "source"
        runner(
            [
                "git",
                "-C",
                str(ROOT),
                "archive",
                "--format=tar",
                "-o",
                str(archive),
                tag_for(version),
            ]
        )
        with tarfile.open(archive) as tar:
            tar.extractall(source, filter="data")
        runner(
            [
                "docker", "build",
                "-t", f"{IMAGE}:{version}",
                "--build-arg", f"VERSION={version}",
                "--build-arg", f"COMMIT_SHA={sha}",
                "-f", str(source / "apps" / "shoplite" / "Dockerfile"),
                str(source),
            ]
        )  # fmt: skip
    return sha


def current(runner: Runner = run) -> dict | None:
    """The running release, or None."""
    try:
        raw = runner(["docker", "inspect", "--format", "{{json .Config.Env}}", CONTAINER])
    except ReleaseError:
        return None
    env = dict(item.split("=", 1) for item in json.loads(raw) if "=" in item)
    return {"version": env.get("VERSION"), "commit_sha": env.get("COMMIT_SHA")}


def wait_healthy(timeout_s: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            resp = httpx2.get(f"http://127.0.0.1:{PORT}/health", timeout=2)
            if resp.status_code == 200:
                return resp.json()
        except httpx2.HTTPError:
            pass
        time.sleep(1)
    raise ReleaseError(f"ShopLite did not become healthy within {timeout_s:.0f}s")


def record(event: dict) -> None:
    DEPLOY_LOG.parent.mkdir(parents=True, exist_ok=True)
    with DEPLOY_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def deploy(version: str, runner: Runner = run, wait: Callable[[], dict] = wait_healthy) -> dict:
    """Run `version` in place of whatever runs now. Returns the deploy event."""
    try:
        runner(["docker", "image", "inspect", f"{IMAGE}:{version}"])
    except ReleaseError as exc:
        raise ReleaseError(
            f"No image for {version}. Build it first: make release VERSION={version}"
        ) from exc

    previous = current(runner)
    try:
        runner(["docker", "rm", "-f", CONTAINER])
    except ReleaseError:
        pass  # nothing was running
    runner(
        [
            "docker", "run", "-d",
            "--name", CONTAINER,
            "-p", f"127.0.0.1:{PORT}:8000",
            "--mount", f"type=bind,source={ROOT / 'logs'},target=/logs",
            "-e", "ENV=local",
            "-e", "SHOPLITE_LOG_FILE=/logs/shoplite.log",
            f"{IMAGE}:{version}",
        ]
    )  # fmt: skip
    health = wait()

    action = "deploy"
    if (
        previous
        and previous.get("version")
        and version_key(version) < version_key(previous["version"])
    ):
        action = "rollback"
    event = {
        "ts": datetime.now(UTC).isoformat(),
        "level": "INFO",
        "msg": f"{action} completed",
        "service": "shoplite",
        "env": health.get("env", "local"),
        "version": health.get("version", version),
        "commit_sha": health.get("commit_sha"),
        "action": action,
        "previous_version": previous.get("version") if previous else None,
        "previous_commit_sha": previous.get("commit_sha") if previous else None,
    }
    record(event)
    return event


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("release", "deploy"):
        sub.add_parser(name).add_argument("version")
    sub.add_parser("status")
    sub.add_parser("stop")
    args = parser.parse_args()
    try:
        if args.command == "release":
            print(f"Built {IMAGE}:{args.version} from commit {release(args.version)}")
        elif args.command == "deploy":
            event = deploy(args.version)
            print(
                f"{event['action'].title()} done: ShopLite {event['version']} "
                f"({event['commit_sha']}) is running; "
                f"previous: {event['previous_version'] or 'none'}"
            )
        elif args.command == "status":
            now = current()
            print(
                f"Running: {now['version']} ({now['commit_sha']})"
                if now
                else "ShopLite is not running"
            )
        elif args.command == "stop":
            run(["docker", "rm", "-f", CONTAINER])
            print("ShopLite stopped")
    except ReleaseError as exc:
        sys.exit(f"Error: {exc}")


if __name__ == "__main__":
    main()
