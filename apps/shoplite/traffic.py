"""Send steady fake shopper traffic to ShopLite so dashboards have something to show.

Usage: python -m apps.shoplite.traffic --rps 5 --seconds 120
"""

import argparse
import http.client
import json
import secrets
import time
from collections import Counter

PRODUCTS = ["mug", "tee", "cap", "sticker"]


def send(host: str, port: int, method: str, path: str, body: dict | None = None) -> int:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        payload = json.dumps(body) if body is not None else None
        conn.request(method, path, body=payload, headers={"Content-Type": "application/json"})
        return conn.getresponse().status
    except OSError:
        return 0  # ShopLite not reachable
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--rps", type=float, default=5.0, help="requests per second")
    parser.add_argument("--seconds", type=float, default=0, help="0 = run until Ctrl+C")
    args = parser.parse_args()

    statuses: Counter[int] = Counter()
    started = time.monotonic()
    print(f"Sending ~{args.rps:g} req/s to http://{args.host}:{args.port} (Ctrl+C to stop)")
    try:
        while not args.seconds or time.monotonic() - started < args.seconds:
            if secrets.randbelow(4) == 0:
                status = send(args.host, args.port, "GET", "/products")
            else:
                body = {
                    "product_id": secrets.choice(PRODUCTS),
                    "quantity": 1 + secrets.randbelow(3),
                }
                status = send(args.host, args.port, "POST", "/checkout", body)
            statuses[status] += 1
            total = sum(statuses.values())
            if total % 25 == 0:
                summary = ", ".join(
                    f"{'unreachable' if s == 0 else s}: {n}" for s, n in sorted(statuses.items())
                )
                print(f"{total} requests -> {summary}")
            time.sleep(1 / args.rps)
    except KeyboardInterrupt:
        pass
    print(f"Done. Status counts: {dict(statuses)}")


if __name__ == "__main__":
    main()
