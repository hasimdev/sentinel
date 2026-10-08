import json
import logging

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from apps.shoplite.main import create_app
from apps.shoplite.settings import Settings, load_settings
from common.logging_setup import JsonFormatter

TAGS = {"service": "shoplite", "env": "test", "version": "9.9.9", "commit_sha": "abc1234"}


def make_client(fail_rate: float = 0.0) -> TestClient:
    return TestClient(create_app(Settings(**TAGS, fail_rate=fail_rate)))


def test_health_returns_ok_and_tags():
    resp = make_client().get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", **TAGS}


def test_products_lists_catalog():
    resp = make_client().get("/products")
    assert resp.status_code == 200
    ids = {p["id"] for p in resp.json()}
    assert {"mug", "tee", "cap", "sticker"} <= ids


def test_checkout_success():
    resp = make_client().post("/checkout", json={"product_id": "mug", "quantity": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert body["product_id"] == "mug"
    assert body["quantity"] == 2
    assert body["total_cents"] == 2400
    assert body["order_id"]


def test_checkout_unknown_product_is_rejected():
    resp = make_client().post("/checkout", json={"product_id": "spaceship"})
    assert resp.status_code == 404


def test_checkout_invalid_quantity_is_rejected():
    resp = make_client().post("/checkout", json={"product_id": "mug", "quantity": 0})
    assert resp.status_code == 422


def test_fail_rate_one_fails_every_checkout():
    client = make_client(fail_rate=1.0)
    codes = {client.post("/checkout", json={"product_id": "mug"}).status_code for _ in range(20)}
    assert codes == {503}


def test_fail_rate_zero_never_fails():
    client = make_client(fail_rate=0.0)
    codes = {client.post("/checkout", json={"product_id": "mug"}).status_code for _ in range(20)}
    assert codes == {200}


def test_logs_carry_the_four_tags(caplog):
    client = make_client(fail_rate=1.0)
    with caplog.at_level(logging.INFO, logger="shoplite"):
        client.post("/checkout", json={"product_id": "mug"})

    records = [r for r in caplog.records if r.name == "shoplite"]
    assert records, "expected at least one shoplite log record"
    for record in records:
        line = json.loads(JsonFormatter().format(record))
        for key, value in TAGS.items():
            assert line[key] == value
    assert any(r.levelname == "ERROR" for r in records)


def test_settings_read_from_environment(monkeypatch):
    monkeypatch.setenv("ENV", "staging")
    monkeypatch.setenv("COMMIT_SHA", "deadbee")
    monkeypatch.setenv("SHOPLITE_FAIL_RATE", "0.3")
    s = load_settings()
    assert s.env == "staging"
    assert s.commit_sha == "deadbee"
    assert s.fail_rate == 0.3


def test_fail_rate_outside_0_to_1_is_rejected():
    with pytest.raises(ValidationError):
        Settings(fail_rate=1.5)
