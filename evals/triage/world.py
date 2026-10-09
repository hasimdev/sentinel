"""A scripted world per practice incident: fake metric, log and incident sources.

They plug into the SAME read-only toolbox the real agent uses (mcp_server.tools), so the
eval exercises the real tool code, limits and error messages. Only the data is scripted.

The fake metric source understands the PromQL shapes an investigator typically writes:
sum / avg / max / min / count with `by (...)`, rate / irate / increase over a selector,
ratios (a / b), a scalar factor (100 * ...), `or vector(0)`, `offset Nm`, and
histogram_quantile. Anything else returns a clear error, as real Prometheus would for
something it can't evaluate. The fake log source supports a LogQL stream selector plus
line filters (|=, !=, |~, !~) and `| json` field filters.
"""

import json
import re
from dataclasses import dataclass
from datetime import timedelta

from evals.triage.cases import NORMAL_TRAFFIC, NOW, Case
from orchestrator.adapters.base import AdapterError, MetricSeries
from orchestrator.models import Evidence, Incident, LogLine

SERVICE, ENV = "shoplite", "local"
METHODS = {"/checkout": "POST", "/products": "GET"}
DEFAULT_P95 = 0.005
ERROR_LINE_EVERY_S = 15  # one error log line per 15 s while a failure is active


# ---------------------------------------------------------------- state over time


def _deploy_end(case: Case, i: int) -> int | None:
    """Minutes-ago when deploy i stopped serving (None = still serving)."""
    d = case.deploys[i]
    if d.share > 0:
        return None
    later = [x.started_min_ago for x in case.deploys if x.started_min_ago < d.started_min_ago]
    return max(later) if later else 0


def active_deploys(case: Case, at: int) -> list[tuple]:
    """(deploy, share) serving traffic `at` minutes ago."""
    live = []
    for i, d in enumerate(case.deploys):
        end = _deploy_end(case, i)
        if d.started_min_ago >= at and (end is None or end < at):
            live.append(d)
    if not live:
        return []
    now_shares = [d.share for d in live]
    if all(s > 0 for s in now_shares):
        total = sum(now_shares)
        return [(d, d.share / total) for d in live]
    return [(d, 1 / len(live)) for d in live]


def error_for(case: Case, version: str, route: str, at: int):
    for e in case.errors:
        until = e.until_min_ago if e.until_min_ago is not None else -1
        if e.version == version and e.route == route and e.since_min_ago >= at > until:
            return e
    return None


@dataclass
class Series:
    labels: dict
    rps: float


def series_at(case: Case, at: int) -> list[Series]:
    out = []
    traffic = case.traffic
    if case.traffic_changed_min_ago is not None and at > case.traffic_changed_min_ago:
        traffic = NORMAL_TRAFFIC
    for d, share in active_deploys(case, at):
        for route, base in traffic.items():
            rps = base * share
            err = error_for(case, d.version, route, at)
            ratio = err.ratio if err else 0.0
            common = {
                "service": SERVICE,
                "env": ENV,
                "version": d.version,
                "commit_sha": d.commit_sha,
                "route": route,
                "method": METHODS[route],
            }
            if ratio < 1:
                out.append(Series({**common, "status": "200"}, rps * (1 - ratio)))
            if ratio > 0:
                out.append(Series({**common, "status": err.status}, rps * ratio))
    return _merge(out)


def _merge(series: list[Series]) -> list[Series]:
    """Two instances of the same version (a restart) report as one series."""
    merged: dict[tuple, Series] = {}
    for s in series:
        key = tuple(sorted(s.labels.items()))
        if key in merged:
            merged[key].rps += s.rps
        else:
            merged[key] = Series(dict(s.labels), s.rps)
    return list(merged.values())


def series_over(case: Case, at: int, window_s: float) -> list[Series]:
    """Average rates over the window ending `at` minutes ago, as rate()[window] does."""
    minutes = max(1, round(window_s / 60))
    totals: dict[tuple, Series] = {}
    for m in range(at, at + minutes):
        for s in series_at(case, m):
            key = tuple(sorted(s.labels.items()))
            totals.setdefault(key, Series(dict(s.labels), 0.0)).rps += s.rps / minutes
    return list(totals.values())


def p95_at(case: Case, version: str, route: str) -> float:
    if route == "/checkout":
        return case.p95_seconds.get(version, DEFAULT_P95)
    return DEFAULT_P95


# ---------------------------------------------------------------- PromQL subset

_MATCHER = re.compile(r'(\w+)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"')
_UNSUPPORTED = (
    "This query shape isn't supported by the eval's metric fixtures. Supported: "
    "sum/avg/max/min/count by (...), rate/irate/increase, ratios a / b, a scalar factor, "
    "'or vector(0)', 'offset Nm', histogram_quantile."
)


def _matchers(text: str) -> list[tuple[str, str, str]]:
    return [(k, op, v.replace('\\"', '"')) for k, op, v in _MATCHER.findall(text)]


def _matches(labels: dict, matchers) -> bool:
    for key, op, value in matchers:
        actual = labels.get(key, "")
        if op == "=" and actual != value:
            return False
        if op == "!=" and actual == value:
            return False
        if op == "=~" and not re.fullmatch(value, actual):
            return False
        if op == "!~" and re.fullmatch(value, actual):
            return False
    return True


def _split_top(expr: str, op: str) -> list[str]:
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(expr):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == op and depth == 0:
            parts.append(expr[start:i])
            start = i + 1
    parts.append(expr[start:])
    return [p.strip() for p in parts]


def _strip_parens(expr: str) -> str:
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        depth = 0
        for i, ch in enumerate(expr):
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0 and i < len(expr) - 1:
                return expr
        expr = expr[1:-1].strip()
    return expr


def _window_s(expr: str) -> float:
    m = re.search(r"\[(\d+)([smh])\]", expr)
    if not m:
        return 300.0
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)]


def _offset_min(expr: str) -> int:
    m = re.search(r"offset\s+(\d+)([smh])", expr)
    if not m:
        return 0
    return int(int(m.group(1)) * {"s": 1 / 60, "m": 1, "h": 60}[m.group(2)])


def _by(expr: str) -> list[str] | None:
    m = re.search(r"\bby\s*\(([^)]*)\)", expr)
    return [x.strip() for x in m.group(1).split(",") if x.strip()] if m else None


def _eval_part(case: Case, expr: str) -> dict[tuple, float]:
    """Evaluate one side of a ratio. Returns {group key tuple: value}."""
    expr = _strip_parens(re.sub(r"\bor\s+vector\(\s*0\s*\)", "", expr))
    at = _offset_min(expr)
    if "histogram_quantile" in expr:
        group = [g for g in (_by(expr) or []) if g != "le"]
        sel = _matchers(expr)
        out: dict[tuple, float] = {}
        for s in series_at(case, at):
            if not _matches(s.labels, sel):
                continue
            key = tuple((g, s.labels.get(g, "")) for g in group)
            out[key] = max(out.get(key, 0.0), p95_at(case, s.labels["version"], s.labels["route"]))
        return out
    if "shoplite_http_requests_total" not in expr:
        raise AdapterError(_UNSUPPORTED)
    sel = _matchers(expr)
    agg = re.match(r"\s*(sum|avg|max|min|count)\b", expr)
    if not re.search(r"\b(rate|irate|increase)\s*\(", expr):
        raise AdapterError(
            "Counters only grow; wrap the selector in rate(...[5m]) or increase(...)."
        )
    window = _window_s(expr)
    factor = window if re.search(r"\bincrease\s*\(", expr) else 1.0
    rows = [s for s in series_over(case, at, window) if _matches(s.labels, sel)]
    if not agg:
        return {tuple(sorted(s.labels.items())): s.rps * factor for s in rows}
    group = _by(expr) or []
    buckets: dict[tuple, list[float]] = {}
    for s in rows:
        key = tuple((g, s.labels.get(g, "")) for g in group)
        buckets.setdefault(key, []).append(s.rps * factor)
    fn = agg.group(1)
    reduce = {"sum": sum, "avg": lambda v: sum(v) / len(v), "max": max, "min": min, "count": len}
    return {k: float(reduce[fn](v)) for k, v in buckets.items()}


def evaluate(case: Case, query: str) -> list[MetricSeries]:
    expr = query.strip()
    scale = 1.0
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*\*\s*(.+)$", expr, re.S)
    if m:
        scale, expr = float(m.group(1)), m.group(2)
    m = re.match(r"^(.+?)\s*\*\s*(\d+(?:\.\d+)?)\s*$", expr, re.S)
    if m:
        expr, scale = m.group(1), float(m.group(2))
    parts = _split_top(_strip_parens(expr), "/")
    if len(parts) > 2:
        raise AdapterError(_UNSUPPORTED)
    try:
        num = _eval_part(case, parts[0])
        den = _eval_part(case, parts[1]) if len(parts) == 2 else None
    except re.error as exc:
        raise AdapterError(f"Invalid regular expression in query: {exc}") from exc
    if den is None:
        result = num
    else:
        result = {}
        for key, d in den.items():
            if d > 0:
                result[key] = num.get(key, 0.0) / d
    return [
        MetricSeries(labels=dict(key), value=round(v * scale, 6))
        for key, v in sorted(result.items())
    ]


# ---------------------------------------------------------------- logs


def _log(minutes_ago: float, level: str, msg: str, labels: dict, **fields) -> LogLine:
    ts = NOW - timedelta(minutes=minutes_ago)
    body = {"ts": ts.isoformat(), "level": level, "msg": msg, **labels, **fields}
    stream = {
        "job": "shoplite",
        "level": level,
        "service": labels["service"],
        "env": labels["env"],
        "version": labels["version"],
        "commit_sha": labels["commit_sha"],
    }
    return LogLine(ts=ts, line=json.dumps(body), labels=stream)


def all_logs(case: Case) -> list[LogLine]:
    lines = []
    products = ["mug", "tee", "cap", "sticker"]
    for d in case.deploys:
        labels = {"service": SERVICE, "env": ENV, "version": d.version, "commit_sha": d.commit_sha}
        lines.append(
            _log(d.started_min_ago, "INFO", "shoplite started", labels, fail_rate=d.fail_rate)
        )
    for e in case.errors:
        d = next(x for x in case.deploys if x.version == e.version)
        labels = {"service": SERVICE, "env": ENV, "version": e.version, "commit_sha": d.commit_sha}
        end = e.until_min_ago or 0
        t, n = e.since_min_ago * 60, 0
        while t > end * 60:
            lines.append(
                _log(t / 60, "ERROR", e.message, labels, route=e.route, product_id=products[n % 4])
            )
            t -= ERROR_LINE_EVERY_S
            n += 1
    current = case.deploys[-1]
    labels = {
        "service": SERVICE,
        "env": ENV,
        "version": current.version,
        "commit_sha": current.commit_sha,
    }
    for minutes_ago, level, msg, fields in case.extra_logs:
        lines.append(_log(minutes_ago, level, msg, labels, **fields))
    lines.sort(key=lambda line: line.ts, reverse=True)
    return lines


def search(case: Case, query: str, since: timedelta, limit: int) -> list[LogLine]:
    if case.logs_unavailable:
        raise AdapterError("Loki query failed: connection refused (log service unavailable)")
    m = re.match(r"\s*\{([^}]*)\}(.*)$", query, re.S)
    if not m:
        raise AdapterError(
            "Loki rejected the query (400): parse error: expected a {stream selector}"
        )
    selector, pipeline = _matchers(m.group(1)), m.group(2)
    filters = re.findall(r'(\|=|!=|\|~|!~)\s*"((?:[^"\\]|\\.)*)"', pipeline)
    field_filters = re.findall(
        r'\|\s*(?!json\b)(\w+)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"', pipeline
    )
    cutoff = NOW - since
    out = []
    for line in all_logs(case):
        if line.ts < cutoff or not _matches(line.labels, selector):
            continue
        ok = True
        for op, text in filters:
            text = text.replace('\\"', '"')
            hit = (text in line.line) if op in ("|=", "!=") else bool(re.search(text, line.line))
            ok = ok and (hit if op in ("|=", "|~") else not hit)
        if ok and field_filters:
            body = {k: str(v) for k, v in json.loads(line.line).items()}
            ok = _matches(body, field_filters)
        if ok:
            out.append(line)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- the incident


def incident_for(case: Case) -> Incident:
    at = case.alert_min_ago
    opened = NOW - timedelta(minutes=at)
    sel = (
        f'service="{SERVICE}", env="{ENV}", version="{case.alert_version}", '
        f'commit_sha="{case.alert_commit}"'
    )
    req = "shoplite_http_requests_total"

    def ratio(window: str) -> tuple[float, float | None]:
        bad = evaluate(case, f'sum(rate({req}{{{sel}, status=~"5.."}}[{window}] offset {at}m))')
        everything = evaluate(case, f"sum(rate({req}{{{sel}}}[{window}] offset {at}m))")
        rps = everything[0].value if everything else None
        return ((bad[0].value / rps) if (bad and rps) else 0.0), rps

    alert_rate, _ = ratio("1m")  # what the alert rule saw
    error_rate, total_rps = ratio("5m")  # what the orchestrator's evidence query sees
    p95 = evaluate(
        case,
        f"histogram_quantile(0.95, sum by (le) "
        f"(rate(shoplite_http_request_duration_seconds_bucket{{{sel}}}[5m] offset {at}m)))",
    )
    evidence = Evidence(
        gathered_at=opened,
        metrics={
            "error_rate": round(error_rate, 4),
            "requests_per_second": round(total_rps, 4) if total_rps else None,
            "p95_latency_seconds": p95[0].value if p95 else None,
        },
    )
    if case.logs_unavailable:
        evidence.gaps.append(
            "error logs unavailable: Loki query failed: "
            "connection refused (log service unavailable)"
        )
    else:
        errors = [
            line
            for line in all_logs(case)
            if line.labels["level"] == "ERROR"
            and line.labels["version"] == case.alert_version
            and line.ts <= opened
        ]
        evidence.error_logs = errors[:20]
    return Incident(
        id=1,
        fingerprint=f"eval-{case.id}",
        status="open",
        alertname="ShopLiteHighErrorRate",
        severity="critical",
        service=SERVICE,
        env=ENV,
        version=case.alert_version,
        commit_sha=case.alert_commit,
        summary=(
            f"ShopLite error rate is {alert_rate * 100:.1f}% on version {case.alert_version} "
            f"({case.alert_commit})"
        ),
        started_at=opened - timedelta(minutes=1),
        last_seen_at=opened,
        evidence=evidence,
    )


# ---------------------------------------------------------------- adapters for the toolbox


class WorldMetrics:
    def __init__(self, case: Case) -> None:
        self.case = case

    def query(self, query: str) -> list[MetricSeries]:
        return evaluate(self.case, query)

    def instant(self, query: str) -> float | None:
        series = self.query(query)
        return series[0].value if series else None


class WorldLogs:
    def __init__(self, case: Case) -> None:
        self.case = case

    def search(self, query: str, since: timedelta, limit: int) -> list[LogLine]:
        return search(self.case, query, since, limit)


class WorldIncidents:
    def __init__(self, case: Case) -> None:
        self.incident = incident_for(case)

    def list(self) -> list[Incident]:
        return [self.incident]

    def get(self, incident_id: int) -> Incident | None:
        return self.incident if incident_id == self.incident.id else None
