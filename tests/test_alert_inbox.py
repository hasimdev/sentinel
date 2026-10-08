import importlib.util
from pathlib import Path

INBOX = Path(__file__).resolve().parent.parent / "infra" / "alert-inbox" / "inbox.py"
spec = importlib.util.spec_from_file_location("inbox", INBOX)
inbox = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inbox)


def alert(status: str, summary: str) -> dict:
    return {
        "status": status,
        "labels": {"alertname": "ShopLiteHighErrorRate", "severity": "critical"},
        "annotations": {"summary": summary},
    }


def test_firing_alert_becomes_one_readable_line():
    payload = {"alerts": [alert("firing", "ShopLite error rate is 38% on version 0.1.0 (abc1234)")]}
    assert inbox.summarize(payload) == [
        "[FIRING] ShopLiteHighErrorRate severity=critical :: "
        "ShopLite error rate is 38% on version 0.1.0 (abc1234)"
    ]


def test_resolved_alerts_are_shown_too():
    payload = {"alerts": [alert("resolved", "back to normal"), alert("firing", "still bad")]}
    lines = inbox.summarize(payload)
    assert lines[0].startswith("[RESOLVED]")
    assert lines[1].startswith("[FIRING]")


def test_empty_or_odd_payloads_do_not_crash():
    assert inbox.summarize({}) == []
    assert inbox.summarize({"alerts": [{}]}) == ["[UNKNOWN] ? severity=- :: "]
