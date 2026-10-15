"""Release and deploy tooling, with Docker and Git faked (nothing is built or run)."""

import json

import pytest

from deploy import shoplite
from deploy.shoplite import ReleaseError


class FakeShell:
    """Answers git/docker commands from a script and records every command."""

    def __init__(self, tags=None, images=(), running=None):
        self.tags = tags or {}
        self.images = set(images)
        self.running = running  # {"version": .., "commit_sha": ..} or None
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd[:2] == ["git", "-C"] and cmd[3] == "rev-parse":
            tag = cmd[-1].split("^")[0]
            if tag not in self.tags:
                raise ReleaseError("unknown revision")
            return self.tags[tag]
        if cmd[:2] == ["git", "-C"] and cmd[3] == "archive":
            import tarfile

            with tarfile.open(cmd[cmd.index("-o") + 1], "w"):
                pass
            return ""
        if cmd[:3] == ["docker", "image", "inspect"]:
            if cmd[3].split(":")[1] not in self.images:
                raise ReleaseError("No such image")
            return "[]"
        if cmd[:2] == ["docker", "inspect"]:
            if not self.running:
                raise ReleaseError("No such object")
            env = [f"VERSION={self.running['version']}", f"COMMIT_SHA={self.running['commit_sha']}"]
            return json.dumps(env)
        return ""


@pytest.fixture(autouse=True)
def deploy_log(tmp_path, monkeypatch):
    path = tmp_path / "deploys.log"
    monkeypatch.setattr(shoplite, "DEPLOY_LOG", path)
    return path


def healthy(version, sha):
    return lambda: {"status": "ok", "env": "local", "version": version, "commit_sha": sha}


def test_release_builds_from_the_git_tag_with_version_and_commit():
    shell = FakeShell(tags={"shoplite-v0.2.0": "c0ffee1"})
    assert shoplite.release("0.2.0", runner=shell) == "c0ffee1"
    build = next(c for c in shell.calls if c[:2] == ["docker", "build"])
    assert ["-t", "sentinel/shoplite:0.2.0"] == build[2:4]
    assert "VERSION=0.2.0" in build and "COMMIT_SHA=c0ffee1" in build
    assert any(
        c[3] == "archive" and c[-1] == "shoplite-v0.2.0" for c in shell.calls if c[0] == "git"
    )


def test_release_without_a_tag_explains_what_to_do():
    with pytest.raises(ReleaseError, match="No release tag shoplite-v9.9.9"):
        shoplite.release("9.9.9", runner=FakeShell())


def test_deploy_needs_a_built_image():
    with pytest.raises(ReleaseError, match="make release VERSION=0.2.0"):
        shoplite.deploy("0.2.0", runner=FakeShell())


def test_deploy_replaces_the_container_and_records_the_event(deploy_log):
    shell = FakeShell(images={"0.2.0"}, running={"version": "0.1.0", "commit_sha": "aaa1111"})
    event = shoplite.deploy("0.2.0", runner=shell, wait=healthy("0.2.0", "c0ffee1"))
    assert ["docker", "rm", "-f", "sentinel-shoplite"] in shell.calls
    run = next(c for c in shell.calls if c[:2] == ["docker", "run"])
    assert "127.0.0.1:8000:8000" in run  # only reachable from this PC
    assert run[-1] == "sentinel/shoplite:0.2.0"
    assert event["action"] == "deploy" and event["previous_version"] == "0.1.0"
    logged = json.loads(deploy_log.read_text().splitlines()[-1])
    for tag in ("service", "env", "version", "commit_sha"):
        assert logged[tag], tag  # every deploy carries the four tags
    assert logged["msg"] == "deploy completed"


def test_deploying_an_older_version_is_a_rollback(deploy_log):
    shell = FakeShell(images={"0.1.0"}, running={"version": "0.2.0", "commit_sha": "c0ffee1"})
    event = shoplite.deploy("0.1.0", runner=shell, wait=healthy("0.1.0", "aaa1111"))
    assert event["action"] == "rollback"
    assert event["previous_version"] == "0.2.0" and event["version"] == "0.1.0"
    assert json.loads(deploy_log.read_text())["msg"] == "rollback completed"


def test_first_deploy_has_no_previous_version():
    event = shoplite.deploy(
        "0.1.0", runner=FakeShell(images={"0.1.0"}), wait=healthy("0.1.0", "a1")
    )
    assert event["action"] == "deploy" and event["previous_version"] is None


def test_version_ordering_is_numeric_not_alphabetical():
    assert shoplite.version_key("0.10.0") > shoplite.version_key("0.9.0")
