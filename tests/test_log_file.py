import json

from fastapi.testclient import TestClient

from apps.shoplite.main import create_app
from apps.shoplite.settings import Settings, load_settings

TAGS = {"service": "shoplite", "env": "test", "version": "9.9.9", "commit_sha": "abc1234"}


def read_lines(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_logs_are_written_to_file_with_tags(tmp_path):
    log_file = tmp_path / "logs" / "shoplite.log"  # folder doesn't exist yet
    client = TestClient(create_app(Settings(**TAGS, fail_rate=1.0, log_file=str(log_file))))
    client.post("/checkout", json={"product_id": "mug"})

    lines = read_lines(log_file)
    assert any(line["msg"] == "shoplite started" for line in lines)
    errors = [line for line in lines if line["level"] == "ERROR"]
    assert errors, "the injected failure should be logged as ERROR"
    for line in lines:
        for key, value in TAGS.items():
            assert line[key] == value
        assert line["ts"]  # Alloy uses this as the log timestamp


def test_no_log_file_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SHOPLITE_LOG_FILE", raising=False)
    create_app(Settings(**TAGS))
    assert not any(tmp_path.iterdir())


def test_log_file_read_from_environment(monkeypatch):
    monkeypatch.setenv("SHOPLITE_LOG_FILE", "logs/shoplite.log")
    assert load_settings().log_file == "logs/shoplite.log"
