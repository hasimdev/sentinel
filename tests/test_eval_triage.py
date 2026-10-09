"""Tests for the triage eval itself (free: Claude and the marker are simulated)."""

import json
from datetime import timedelta
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from evals.triage import world
from evals.triage.cases import CASES
from evals.triage.grader import Judge, rec_grade
from evals.triage.run import run_one, to_trace

CASE = {c.id: c for c in CASES}


# --- the case set ---


def test_case_set_is_balanced_and_consistent():
    assert len(CASES) == 16
    assert len({c.id for c in CASES}) == 16
    answers = [c.expected for c in CASES]
    assert {a: answers.count(a) for a in set(answers)} == {
        "rollback": 5,
        "investigate": 7,
        "no_action": 4,
    }
    for c in CASES:
        assert c.tags[0] == c.expected
        # Every alert is above the 20% line at the moment it fired, as in real life.
        summary = world.incident_for(c).summary
        assert float(summary.split("error rate is ")[1].split("%")[0]) > 20, c.id


def test_the_ai_never_sees_the_answer_key():
    for c in CASES:
        visible = world.incident_for(c).model_dump_json() + "".join(
            line.line for line in world.all_logs(c)
        )
        assert c.cause not in visible and c.story not in visible, c.id


# --- the scripted world ---


def test_error_ratio_by_version():
    q = (
        'sum by (version) (rate(shoplite_http_requests_total{status=~"5.."}[5m]))'
        " / sum by (version) (rate(shoplite_http_requests_total[5m]))"
    )
    got = {s.labels["version"]: round(s.value, 2) for s in world.evaluate(CASE["canary-split"], q)}
    assert got == {"0.1.0": 0.0, "0.2.0": 0.45}


def test_offset_looks_into_the_past():
    c = CASE["already-rolled-back"]
    q = "sum by (version) (rate(shoplite_http_requests_total[5m] offset {}m))"
    assert {s.labels["version"] for s in world.evaluate(c, q.format(15))} == {"0.2.0"}
    assert {s.labels["version"] for s in world.evaluate(c, q.format(0))} == {"0.1.0"}


def test_scalar_factor_and_or_vector():
    c = CASE["blip-recovered"]
    q = (
        '100 * (sum(rate(shoplite_http_requests_total{status=~"5.."}[5m])) or vector(0))'
        " / sum(rate(shoplite_http_requests_total[5m]))"
    )
    assert [s.value for s in world.evaluate(c, q)] == [0.0]


def test_latency_quantile():
    q = (
        "histogram_quantile(0.95, sum by (le) "
        "(rate(shoplite_http_request_duration_seconds_bucket[5m])))"
    )
    assert world.evaluate(CASE["bad-release-slow-db"], q)[0].value == 2.1


def test_windows_include_recent_history():
    # Errors ran from 16 to 12 minutes ago: a 15-minute window sees them, a 5-minute one doesn't.
    c = CASE["blip-recovered"]
    q = 'sum(increase(shoplite_http_requests_total{{status=~"5.."}}[{}m]))'
    assert world.evaluate(c, q.format(15))[0].value > 0
    assert not world.evaluate(c, q.format(5))


def test_traffic_surge_is_visible_over_time():
    c = CASE["traffic-spike"]
    q = "sum(rate(shoplite_http_requests_total[1m] offset {}m))"
    before, after = (world.evaluate(c, q.format(m))[0].value for m in (30, 0))
    assert round(after / before) == 5


def test_unsupported_query_is_a_clear_error():
    with pytest.raises(world.AdapterError, match="isn't supported"):
        world.evaluate(CASE["canary-split"], "max_over_time(up[1h])")


def test_log_search_filters_and_window():
    c = CASE["feature-flag"]
    lines = world.search(c, '{service="shoplite"} |= "feature flag"', timedelta(hours=1), 10)
    assert len(lines) == 1 and "new_checkout_flow" in lines[0].line
    errors = world.search(
        c, '{level="ERROR"} | json | route="/checkout"', timedelta(minutes=5), 100
    )
    assert errors and all(e.ts >= world.NOW - timedelta(minutes=5) for e in errors)


def test_logs_down_case_raises():
    with pytest.raises(world.AdapterError, match="unavailable"):
        world.search(CASE["logs-unavailable"], "{}", timedelta(minutes=5), 5)


# --- marking ---


def test_rec_grade_and_false_rollback():
    assert rec_grade("rollback", "rollback") == {"rec_correct": 1.0, "false_rollback": 0.0}
    assert rec_grade("investigate", "rollback") == {"rec_correct": 0.0, "false_rollback": 1.0}
    assert rec_grade("no_action", None) == {"rec_correct": 0.0, "false_rollback": 0.0}


class FakeMarker:
    """Says 'correct' only when the diagnosis cause contains the answer key's first 3 words."""

    def __init__(self):
        self.calls = 0
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        content = kwargs["messages"][0]["content"]
        key = content.split("Answer key (correct cause): ")[1].split("\n")[0]
        ok = " ".join(key.split()[:3]) in content.split("<diagnosis>")[1]
        text = json.dumps({"cause_correct": ok, "explanation": "matches" if ok else "differs"})
        return SimpleNamespace(
            model="claude-sonnet-5-5",
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=500, output_tokens=50),
        )


# --- the whole runner, with a simulated Claude ---


def scripted_claude(answer):
    """A fake Claude: one get_incident look-up, then the given diagnosis (or an exception)."""

    def reply(content, stop):
        return SimpleNamespace(
            model="claude-opus-5-5",
            content=content,
            stop_reason=stop,
            usage=SimpleNamespace(input_tokens=1000, output_tokens=100),
        )

    state = {"n": 0}

    def create(**kwargs):
        state["n"] += 1
        if isinstance(answer, Exception):
            raise answer
        if state["n"] == 1:
            call = SimpleNamespace(
                type="tool_use", id="t1", name="get_incident", input={"incident_id": 1}
            )
            return reply([call], "tool_use")
        return reply([SimpleNamespace(type="text", text=json.dumps(answer))], "end_turn")

    return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))


def diagnosis(case, recommendation):
    return {
        "likely_cause": case.cause,
        "recommendation": recommendation,
        "reasoning": "r",
        "confidence": "high",
        "evidence": ["e"],
    }


def run_all(tmp_path, answer_for):
    marker = Judge(client=FakeMarker())
    for case in CASES:
        run_one(
            case, 0, tmp_path, marker, "claude-opus-5-5", client=scripted_claude(answer_for(case))
        )
    path = tmp_path / "results.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def test_oracle_scores_100_percent(tmp_path):
    rows = run_all(tmp_path, lambda c: diagnosis(c, c.expected))
    assert len(rows) == 16
    assert all(
        r["grade"] == {"rec_correct": 1.0, "false_rollback": 0.0, "cause_correct": 1.0}
        for r in rows
    )
    trace = json.loads((tmp_path / "traces" / "fault-injection_rep0.json").read_text())
    assert [t["role"] for t in trace][:4] == ["system", "user", "tool_call", "tool_result"]


def test_always_rollback_scores_badly(tmp_path):
    rows = run_all(tmp_path, lambda c: diagnosis(c, "rollback"))
    right = sum(r["grade"]["rec_correct"] for r in rows)
    wrong_rollbacks = sum(r["grade"]["false_rollback"] for r in rows)
    assert right == 5 and wrong_rollbacks == 11


def test_wrong_cause_is_marked_wrong(tmp_path):
    def wrong(c):
        d = diagnosis(c, c.expected)
        d["likely_cause"] = "Something is wrong with the shop."
        return d

    rows = run_all(tmp_path, wrong)
    assert all(r["grade"]["cause_correct"] == 0.0 for r in rows)


def test_api_errors_go_to_the_error_log_not_the_score(tmp_path):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.InternalServerError(
        "boom", response=httpx2.Response(500, request=request), body=None
    )
    rows = run_all(tmp_path, lambda c: err)
    assert rows == []
    errors = [json.loads(x) for x in (tmp_path / "errors.jsonl").read_text().splitlines()]
    assert len(errors) == 16 and {e["class"] for e in errors} == {"api-error"}


def test_eval_tells_the_ai_the_scripted_time(tmp_path):
    run_all(tmp_path, lambda c: diagnosis(c, c.expected))
    trace = json.loads((tmp_path / "traces" / "blip-recovered_rep0.json").read_text())
    assert "Current time: 2026-10-08T10:30:00+00:00" in trace[1]["content"]


def test_to_trace_handles_plain_text():
    assert to_trace([{"role": "user", "content": "hi"}])[1] == {"role": "user", "content": "hi"}
