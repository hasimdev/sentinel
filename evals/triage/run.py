"""Run the triage eval: every practice incident through the REAL triage agent, then mark it.

Usage (from the repo root):
  python -m evals.triage.run --approve-harness     # once, by a person, after reviewing changes
  python -m evals.triage.run --cases 4 --reps 1    # pilot
  python -m evals.triage.run --reps 2              # full run (16 incidents x 2)

Output: .claude/hillclimb/triage/<variant>/  results.jsonl, errors.jsonl, traces/
Re-running resumes: finished (incident, rep) pairs are skipped.
"""

import argparse
import hashlib
import json
import math
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

from evals.triage.cases import CASES, NOW, Case
from evals.triage.grader import JUDGE_MODEL, Judge, rec_grade
from evals.triage.world import WorldIncidents, WorldLogs, WorldMetrics
from mcp_server.tools import make_toolbox
from orchestrator import triage
from orchestrator.triage import SYSTEM_PROMPT, TriageAgent

ROOT = Path(__file__).resolve().parents[2]
FLOW = ROOT / ".claude" / "hillclimb" / "triage"
HARNESS_FILES = [
    "evals/triage/run.py",
    "evals/triage/world.py",
    "evals/triage/grader.py",
    "evals/triage/cases.py",
    "orchestrator/triage.py",
    "mcp_server/tools.py",
]
PRICES = {  # USD per million tokens (input, output), first-party API list prices
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
}
STATE = {
    "metrics": [
        {"id": "rec_correct", "label": "Right call", "kind": "binary"},
        {"id": "cause_correct", "label": "Right cause", "kind": "binary"},
        {"id": "false_rollback", "label": "Wrong rollback", "kind": "binary", "better": "lower"},
    ],
    "perf_fields": [
        {"id": "latency_s", "label": "Time (s)"},
        {"id": "tool_calls", "label": "Look-ups"},
    ],
}
_write_lock = threading.Lock()


def harness_sha() -> str:
    h = hashlib.sha256()
    for rel in HARNESS_FILES:
        h.update(rel.encode())
        h.update((ROOT / rel).read_bytes())
    return h.hexdigest()


def check_harness(approve: bool) -> None:
    record = FLOW / "harness_approval.json"
    sha = harness_sha()
    if approve:
        FLOW.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({"sha": sha, "files": HARNESS_FILES}, indent=2))
        print(f"Harness approved ({sha[:12]}). You can now run the eval.")
        sys.exit(0)
    approved = json.loads(record.read_text())["sha"] if record.exists() else None
    if approved != sha:
        print(
            "The eval's code has changed since it was last approved (or was never approved).\n"
            "Review the changes, then run:  python -m evals.triage.run --approve-harness",
            file=sys.stderr,
        )
        sys.exit(2)


def done_keys(out: Path) -> set[tuple[str, int]]:
    path = out / "results.jsonl"
    if not path.exists():
        return set()
    return {(r["prompt_id"], r["rep"]) for r in map(json.loads, path.read_text().splitlines())}


def append(path: Path, row: dict) -> None:
    with _write_lock, path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def to_trace(messages: list[dict]) -> list[dict]:
    """The agent's conversation in the report's trace format."""
    trace = [{"role": "system", "content": SYSTEM_PROMPT}]
    for msg in messages:
        content = msg["content"]
        if isinstance(content, str):
            trace.append({"role": msg["role"], "content": content})
            continue
        for block in content:
            kind = block["type"] if isinstance(block, dict) else block.type
            if kind == "tool_result":
                trace.append({"role": "tool_result", "content": block["content"]})
            elif kind == "tool_use":
                trace.append(
                    {
                        "role": "tool_call",
                        "name": block.name,
                        "content": json.dumps(block.input, indent=2),
                    }
                )
            elif kind == "text":
                trace.append({"role": "assistant", "content": block.text})
    return trace


def run_one(case: Case, rep: int, out: Path, judge: Judge, model: str, client=None) -> None:
    incidents = WorldIncidents(case)
    agent = TriageAgent(make_toolbox(incidents, WorldMetrics(case), WorldLogs(case)), client=client)
    incident = incidents.incident
    started = time.monotonic()
    result = agent.triage(incident, now=NOW)
    latency = round(time.monotonic() - started, 2)
    usage = {"input_tokens": result.input_tokens, "output_tokens": result.output_tokens}
    base = {"prompt_id": case.id, "rep": rep, "model": result.model, "usage": usage}

    error = result.error or ""
    if result.model != model:
        append(
            out / "errors.jsonl", {**base, "class": "served-model mismatch", "error": result.model}
        )
        return
    if result.status == "failed" and error.startswith(("Anthropic", "Could not reach")):
        append(out / "errors.jsonl", {**base, "class": "api-error", "error": error})
        return

    trace = to_trace(agent.last_messages)
    (out / "traces").mkdir(parents=True, exist_ok=True)
    (out / "traces" / f"{case.id}_rep{rep}.json").write_text(json.dumps(trace, indent=2))

    row = {
        **base,
        "prompt": f"{case.title}. Incident: {incident.summary}",
        "tags": [case.expected, case.tags[1]],
        "stop_reason": "end_turn",
        "status": "ok",
        "latency_s": latency,
        "tool_calls": len(result.tool_calls),
        "meta": {"answer_key": case.cause, "error": result.error},
    }
    if "cut off" in error:
        append(
            out / "results.jsonl",
            {**row, "status": "truncated", "stop_reason": "max_tokens", "grade": {}},
        )
        return

    diagnosis = result.diagnosis.model_dump() if result.diagnosis else None
    grade = rec_grade(case.expected, diagnosis["recommendation"] if diagnosis else None)
    explanation = {
        "rec_correct": f"expected {case.expected}, got "
        f"{diagnosis['recommendation'] if diagnosis else 'no diagnosis'}"
    }
    if diagnosis is None:
        grade["cause_correct"] = 0.0
        explanation["cause_correct"] = f"no diagnosis ({error})"
    else:
        try:
            ok, why, judge_meta = judge.grade_cause(case.cause, diagnosis)
        except Exception as exc:  # a marker failure is plumbing, not a model miss
            append(out / "errors.jsonl", {**base, "class": "grader-error", "error": str(exc)})
            return
        grade["cause_correct"] = float(ok)
        explanation["cause_correct"] = why
        row["judge_model"] = judge_meta["model"]
        row["judge_usage"] = judge_meta["usage"]
    row["meta"]["diagnosis"] = diagnosis
    if error and "declined" in error:
        row["meta"]["refusal"] = True
    append(out / "results.jsonl", {**row, "grade": grade, "explanation": explanation})


def run_with_ceiling(case, rep, out, judge, model, timeout_s) -> None:
    worker = threading.Thread(target=run_one, args=(case, rep, out, judge, model), daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        append(
            out / "errors.jsonl",
            {
                "prompt_id": case.id,
                "rep": rep,
                "class": "timeout",
                "error": f"no result within {timeout_s}s",
            },
        )


def cost(rows: list[dict]) -> float:
    total = 0.0
    for r in rows:
        for m, u in (
            (r.get("model"), r.get("usage")),
            (r.get("judge_model"), r.get("judge_usage")),
        ):
            if m in PRICES and u:
                p_in, p_out = PRICES[m]
                total += u["input_tokens"] * p_in / 1e6 + u["output_tokens"] * p_out / 1e6
    return total


def summarize(out: Path) -> None:
    rows = [json.loads(x) for x in (out / "results.jsonl").read_text().splitlines()]
    ok = [r for r in rows if r["status"] == "ok"]
    errors = (
        (out / "errors.jsonl").read_text().splitlines() if (out / "errors.jsonl").exists() else []
    )
    print(
        f"\n{len(ok)} graded runs ({len(rows) - len(ok)} truncated, {len(errors)} errors) in {out}"
    )
    for m in ("rec_correct", "cause_correct", "false_rollback"):
        vals = [r["grade"][m] for r in ok]
        p = sum(vals) / len(vals)
        half = 1.96 * math.sqrt(p * (1 - p) / len(vals)) if 0 < p < 1 else 0.0
        print(f"  {m:15} {p:6.1%}  (95% CI +/- {half:.0%}, n={len(vals)})")
    for label in ("rollback", "investigate", "no_action"):
        group = [r for r in ok if r["tags"][0] == label]
        if group:
            got = statistics.mean(r["grade"]["rec_correct"] for r in group)
            print(f"  right call when answer is {label:11} {got:6.1%} (n={len(group)})")
    lat = [r["latency_s"] for r in ok]
    tools = [r["tool_calls"] for r in ok]
    print(f"  time per diagnosis: median {statistics.median(lat):.0f}s (max {max(lat):.0f}s)")
    print(f"  look-ups: median {statistics.median(tools):.0f} (max {max(tools)})")
    print(
        f"  measured cost (agent + marker): ${cost(rows):.2f}  "
        f"(${cost(rows) / max(len(rows), 1):.3f} per run)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--variant", default="baseline")
    parser.add_argument("--reps", type=int, default=2)
    parser.add_argument("--cases", type=int, default=0, help="first N incidents only (0 = all)")
    parser.add_argument("--only", default="", help="comma-separated incident ids")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout-s", type=int, default=300)
    parser.add_argument("--approve-harness", action="store_true")
    args = parser.parse_args()

    check_harness(args.approve_harness)
    load_dotenv(ROOT / ".env")
    out = FLOW / args.variant
    out.mkdir(parents=True, exist_ok=True)
    (FLOW / "_state.json").write_text(json.dumps(STATE, indent=2))

    cases = CASES[: args.cases] if args.cases else CASES
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in CASES if c.id in wanted]
    done = done_keys(out)
    todo = [(c, r) for c in cases for r in range(args.reps) if (c.id, r) not in done]
    print(
        f"{len(todo)} runs to do on {triage.MODEL} (marker: {JUDGE_MODEL}); "
        f"{len(done)} already done"
    )
    judge = Judge()
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for case, rep in todo:
            pool.submit(run_with_ceiling, case, rep, out, judge, triage.MODEL, args.timeout_s)
    print(f"wall-clock: {time.monotonic() - started:.0f}s")
    if (out / "results.jsonl").exists():
        summarize(out)


if __name__ == "__main__":
    main()
