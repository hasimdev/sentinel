"""Practice incidents for the triage eval, each with a known right answer.

Each case is a small scripted world: what is deployed, what is failing, what the logs say.
The eval's fake look-up tools answer from this world instead of live Prometheus/Loki, so
every run sees exactly the same evidence. Times are minutes before NOW.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

NOW = datetime(2026, 10, 8, 10, 30, tzinfo=UTC)

# Normal traffic per route (requests per second), as the traffic generator sends it.
NORMAL_TRAFFIC = {"/checkout": 3.6, "/products": 1.2}


@dataclass
class Deploy:
    version: str
    commit_sha: str
    started_min_ago: int  # when this version's instances started (from their startup log)
    share: float = 1.0  # share of traffic it serves now; 0 = no longer running
    fail_rate: float = 0.0  # the "break it" dial as logged at startup


@dataclass
class Errors:
    version: str
    route: str
    ratio: float  # share of that version's requests on that route that fail
    message: str
    since_min_ago: int
    until_min_ago: int | None = None  # None = still failing now
    status: str = "503"


@dataclass
class Case:
    id: str
    title: str
    story: str  # plain-English description for the human reviewer (not shown to the AI)
    expected: str  # rollback | investigate | no_action
    cause: str  # the correct cause in one line (used by the AI marker)
    tags: list[str]
    alert_version: str
    alert_commit: str
    alert_min_ago: int  # when the incident opened
    deploys: list[Deploy]
    errors: list[Errors]
    traffic: dict[str, float] = field(default_factory=lambda: dict(NORMAL_TRAFFIC))
    traffic_changed_min_ago: int | None = None  # before this, traffic was NORMAL_TRAFFIC
    p95_seconds: dict[str, float] = field(default_factory=dict)  # by version; default 0.005
    extra_logs: list[tuple[int, str, str, dict]] = field(default_factory=list)
    logs_unavailable: bool = False


CASES = [
    # ---------------- rollback: a specific release broke things ----------------
    Case(
        id="bad-release-checkout",
        title="New release breaks checkout",
        story="Version 0.2.0 went out 25 minutes ago. Since then 45% of checkouts fail with a "
        "payment error. Version 0.1.0 ran cleanly for days before.",
        expected="rollback",
        cause="Release 0.2.0 (b7e41c2) introduced a checkout bug; 0.1.0 had no errors.",
        tags=["rollback", "clear"],
        alert_version="0.2.0",
        alert_commit="b7e41c2",
        alert_min_ago=20,
        deploys=[
            Deploy("0.1.0", "a1c9f03", 4000, share=0.0),
            Deploy("0.2.0", "b7e41c2", 25),
        ],
        errors=[
            Errors(
                "0.2.0",
                "/checkout",
                0.45,
                "checkout failed: TypeError: 'NoneType' object is not subscriptable "
                "in payment_client.charge",
                24,
            )
        ],
    ),
    Case(
        id="canary-split",
        title="Half the traffic on the new version, only it fails",
        story="0.1.0 and 0.2.0 are both running, each taking half the traffic. Only 0.2.0's "
        "checkouts fail (60%); 0.1.0 is fine.",
        expected="rollback",
        cause="Release 0.2.0 (c41d8e7) fails checkouts; 0.1.0 serving the other half is healthy.",
        tags=["rollback", "split-traffic"],
        alert_version="0.2.0",
        alert_commit="c41d8e7",
        alert_min_ago=12,
        deploys=[
            Deploy("0.1.0", "a1c9f03", 4000, share=0.5),
            Deploy("0.2.0", "c41d8e7", 18, share=0.5),
        ],
        errors=[
            Errors(
                "0.2.0",
                "/checkout",
                0.6,
                "checkout failed: KeyError: 'currency' in pricing.total",
                17,
            )
        ],
    ),
    Case(
        id="bad-release-products",
        title="New release breaks the product list",
        story="Right after 0.3.0 deployed, the product list started failing for every visitor "
        "(500 errors). Checkout still works for people who already have a product page open.",
        expected="rollback",
        cause=(
            "Release 0.3.0 (d93a210) broke /products (KeyError on price "
            "field) right after deploy."
        ),
        tags=["rollback", "other-route"],
        alert_version="0.3.0",
        alert_commit="d93a210",
        alert_min_ago=8,
        deploys=[
            Deploy("0.2.0", "c41d8e7", 3000, share=0.0),
            Deploy("0.3.0", "d93a210", 10),
        ],
        errors=[
            Errors(
                "0.3.0",
                "/products",
                1.0,
                "list products failed: KeyError: 'price_cents' in catalog.serialize",
                10,
                status="500",
            )
        ],
    ),
    Case(
        id="bad-release-slow-db",
        title="New release makes checkout time out",
        story="0.2.1 added a new database query. Since it deployed, checkouts take over 2 seconds "
        "and 30% time out (504). Before, checkout took about 5 ms.",
        expected="rollback",
        cause="Release 0.2.1 (e5f6071) added a slow query; checkouts time out since that deploy.",
        tags=["rollback", "latency"],
        alert_version="0.2.1",
        alert_commit="e5f6071",
        alert_min_ago=15,
        deploys=[
            Deploy("0.2.0", "c41d8e7", 2500, share=0.0),
            Deploy("0.2.1", "e5f6071", 19),
        ],
        errors=[
            Errors(
                "0.2.1",
                "/checkout",
                0.3,
                "checkout failed: database query timed out after 2.0s (orders.lookup_by_user)",
                18,
                status="504",
            )
        ],
        p95_seconds={"0.2.1": 2.1},
    ),
    Case(
        id="bad-release-borderline",
        title="New release, error rate just over the line",
        story="After 0.4.0 deployed, 30% of checkouts fail with a new validation error that "
        "never appeared before. Overall that's 22.5%: just above the 20% alert line, but "
        "clearly new.",
        expected="rollback",
        cause="Release 0.4.0 (f1029bd) added a validation bug rejecting valid checkouts.",
        tags=["rollback", "borderline"],
        alert_version="0.4.0",
        alert_commit="f1029bd",
        alert_min_ago=9,
        deploys=[
            Deploy("0.3.1", "d93a777", 5000, share=0.0),
            Deploy("0.4.0", "f1029bd", 14),
        ],
        errors=[
            Errors(
                "0.4.0",
                "/checkout",
                0.3,
                "checkout failed: ValidationError: quantity must be <= 0 (validators.order)",
                13,
            )
        ],
    ),
    # ---------------- investigate: rolling back wouldn't fix it ----------------
    Case(
        id="fault-injection",
        title="Someone turned on the 'break it' dial",
        story="Half of checkouts fail with 'injected fault'. The startup log shows fail_rate=0.5 "
        "on both the current and the previous release, which failed just as much.",
        expected="investigate",
        cause=(
            "Fault injection is switched on (fail_rate=0.5); not a code "
            "bug, rollback won't help."
        ),
        tags=["investigate", "config"],
        alert_version="0.2.0",
        alert_commit="b7e41c2",
        alert_min_ago=10,
        deploys=[
            Deploy("0.1.0", "a1c9f03", 60, share=0.0, fail_rate=0.5),
            Deploy("0.2.0", "b7e41c2", 30, fail_rate=0.5),
        ],
        errors=[
            Errors("0.1.0", "/checkout", 0.5, "checkout failed: injected fault", 60, 30),
            Errors("0.2.0", "/checkout", 0.5, "checkout failed: injected fault", 30),
        ],
    ),
    Case(
        id="payment-provider-outage",
        title="The payment provider is down",
        story="Checkouts started failing 15 minutes ago with timeouts calling the outside payment "
        "gateway. The current release has run fine for 2 days; nothing was deployed today.",
        expected="investigate",
        cause="External payment gateway outage; current release ran cleanly for 2 days.",
        tags=["investigate", "dependency"],
        alert_version="0.2.0",
        alert_commit="b7e41c2",
        alert_min_ago=13,
        deploys=[Deploy("0.2.0", "b7e41c2", 2900)],
        errors=[
            Errors(
                "0.2.0",
                "/checkout",
                0.55,
                "checkout failed: upstream payment-gateway.example.net timed out after 3s",
                15,
                status="502",
            )
        ],
    ),
    Case(
        id="logs-unavailable",
        title="Errors on a long-running version, logs are down",
        story="Checkout errors at 35% on a version that has run for 3 days. The log system is "
        "down, so the error messages can't be read. Metrics only.",
        expected="investigate",
        cause=(
            "Errors on a 3-day-old release with no recent deploy; logs "
            "unavailable, cause unknown."
        ),
        tags=["investigate", "missing-evidence"],
        alert_version="0.2.0",
        alert_commit="b7e41c2",
        alert_min_ago=11,
        deploys=[Deploy("0.2.0", "b7e41c2", 4300)],
        errors=[Errors("0.2.0", "/checkout", 0.35, "checkout failed: unknown", 14)],
        logs_unavailable=True,
    ),
    Case(
        id="traffic-spike",
        title="A sudden traffic surge overloads the shop",
        story="Traffic jumped to 5 times normal 12 minutes ago (a marketing email went out). "
        "Checkouts fail with 'connection pool exhausted'. No deploy for 2 days.",
        expected="investigate",
        cause=(
            "Traffic surge (about 5x) exhausted the database connection "
            "pool; not a release issue."
        ),
        tags=["investigate", "capacity"],
        alert_version="0.2.0",
        alert_commit="b7e41c2",
        alert_min_ago=10,
        deploys=[Deploy("0.2.0", "b7e41c2", 2950)],
        traffic={"/checkout": 18.0, "/products": 6.0},
        traffic_changed_min_ago=12,
        errors=[
            Errors(
                "0.2.0",
                "/checkout",
                0.4,
                "checkout failed: database connection pool exhausted (max 20 connections)",
                12,
            )
        ],
        p95_seconds={"0.2.0": 0.9},
    ),
    Case(
        id="old-version-failing",
        title="The failing instances run the OLD version",
        story="0.3.0 is healthy. A few leftover instances still run 0.2.0, and those are the ones "
        "failing. Rolling back to 0.2.0 would make things worse.",
        expected="investigate",
        cause=(
            "Leftover 0.2.0 instances are failing; the current 0.3.0 is "
            "healthy, so rollback is wrong."
        ),
        tags=["investigate", "trap"],
        alert_version="0.2.0",
        alert_commit="c41d8e7",
        alert_min_ago=10,
        deploys=[
            Deploy("0.2.0", "c41d8e7", 3000, share=0.3),
            Deploy("0.3.0", "d93a210", 120, share=0.7),
        ],
        errors=[
            Errors(
                "0.2.0",
                "/checkout",
                0.9,
                "checkout failed: schema mismatch: column orders.currency does not exist",
                14,
            )
        ],
    ),
    Case(
        id="errors-before-release",
        title="Errors started before the latest release",
        story="Checkout errors began 40 minutes ago on 0.2.0. 0.2.1 deployed 20 minutes ago and "
        "fails at the same rate. The release didn't cause it.",
        expected="investigate",
        cause="Errors began 20 minutes before 0.2.1 deployed and continue at the same rate.",
        tags=["investigate", "timing"],
        alert_version="0.2.1",
        alert_commit="e5f6071",
        alert_min_ago=8,
        deploys=[
            Deploy("0.2.0", "c41d8e7", 3000, share=0.0),
            Deploy("0.2.1", "e5f6071", 20),
        ],
        errors=[
            Errors(
                "0.2.0",
                "/checkout",
                0.4,
                "checkout failed: inventory-service returned 503 Service Unavailable",
                40,
                20,
            ),
            Errors(
                "0.2.1",
                "/checkout",
                0.4,
                "checkout failed: inventory-service returned 503 Service Unavailable",
                20,
            ),
        ],
    ),
    Case(
        id="feature-flag",
        title="A feature flag switched on, no new code",
        story="No deploy today. 18 minutes ago a log line shows the 'new_checkout_flow' feature "
        "flag was enabled; checkout errors started at the same moment.",
        expected="investigate",
        cause="Enabling the new_checkout_flow feature flag broke checkout; no code was deployed.",
        tags=["investigate", "config"],
        alert_version="0.2.0",
        alert_commit="b7e41c2",
        alert_min_ago=14,
        deploys=[Deploy("0.2.0", "b7e41c2", 2800)],
        errors=[
            Errors(
                "0.2.0",
                "/checkout",
                0.5,
                "checkout failed: AttributeError: 'Cart' object has no attribute 'promo_code' "
                "(new_checkout_flow)",
                18,
            )
        ],
        extra_logs=[
            (18, "INFO", "feature flag changed", {"flag": "new_checkout_flow", "enabled": True}),
        ],
    ),
    # ---------------- no_action: it has already cleared ----------------
    Case(
        id="blip-recovered",
        title="A short blip that cleared by itself",
        story="Checkouts failed for about 3 minutes, then stopped on their own. No errors for the "
        "last 12 minutes and no deploy. Nothing left to do.",
        expected="no_action",
        cause="A 3-minute blip that cleared by itself; no errors for the last 12 minutes.",
        tags=["no_action", "recovered"],
        alert_version="0.2.0",
        alert_commit="b7e41c2",
        alert_min_ago=14,
        deploys=[Deploy("0.2.0", "b7e41c2", 2700)],
        errors=[
            Errors(
                "0.2.0",
                "/checkout",
                0.5,
                "checkout failed: redis cache connection reset",
                16,
                12,
            )
        ],
    ),
    Case(
        id="already-rolled-back",
        title="The bad release was already rolled back",
        story="0.2.0 broke checkout, then someone redeployed 0.1.0 eight minutes ago. Errors "
        "stopped right after. The problem is already handled.",
        expected="no_action",
        cause="Bad release 0.2.0 was already rolled back to 0.1.0; errors stopped 8 minutes ago.",
        tags=["no_action", "recovered"],
        alert_version="0.2.0",
        alert_commit="b7e41c2",
        alert_min_ago=18,
        deploys=[
            Deploy("0.2.0", "b7e41c2", 25, share=0.0),
            Deploy("0.1.0", "a1c9f03", 8),
        ],
        errors=[
            Errors(
                "0.2.0",
                "/checkout",
                0.45,
                "checkout failed: TypeError: 'NoneType' object is not subscriptable "
                "in payment_client.charge",
                24,
                8,
            )
        ],
    ),
    Case(
        id="chaos-test-ended",
        title="A chaos test ended and the dial was turned off",
        story="Fault injection was on for a test. Ten minutes ago the shop restarted with "
        "fail_rate=0 and errors stopped.",
        expected="no_action",
        cause=(
            "Fault injection was turned off (restart with fail_rate=0); "
            "errors stopped 10 minutes ago."
        ),
        tags=["no_action", "config"],
        alert_version="0.2.0",
        alert_commit="b7e41c2",
        alert_min_ago=20,
        deploys=[
            Deploy("0.2.0", "b7e41c2", 40, share=0.0, fail_rate=0.5),
            Deploy("0.2.0", "b7e41c2", 10, fail_rate=0.0),
        ],
        errors=[Errors("0.2.0", "/checkout", 0.5, "checkout failed: injected fault", 40, 10)],
    ),
    Case(
        id="provider-recovered",
        title="The payment provider came back",
        story="The outside payment gateway timed out for 15 minutes, then recovered 9 minutes ago. "
        "Checkouts have succeeded since.",
        expected="no_action",
        cause=(
            "External payment gateway outage that recovered 9 minutes "
            "ago; checkouts healthy now."
        ),
        tags=["no_action", "dependency"],
        alert_version="0.2.0",
        alert_commit="b7e41c2",
        alert_min_ago=20,
        deploys=[Deploy("0.2.0", "b7e41c2", 3100)],
        errors=[
            Errors(
                "0.2.0",
                "/checkout",
                0.55,
                "checkout failed: upstream payment-gateway.example.net timed out after 3s",
                24,
                9,
                status="502",
            )
        ],
    ),
]
