"""Tiny alert inbox: receives Alertmanager webhooks and prints one line per alert.

Placeholder until the orchestrator exists. Standard library only, so it runs in a plain
python image with nothing installed. See it with `make alerts`.
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer


def summarize(payload: dict) -> list[str]:
    """Turn an Alertmanager webhook payload into one readable line per alert."""
    lines = []
    for alert in payload.get("alerts", []):
        labels = alert.get("labels", {})
        annotations = alert.get("annotations", {})
        status = alert.get("status", "unknown").upper()
        name = labels.get("alertname", "?")
        severity = labels.get("severity", "-")
        text = annotations.get("summary", "")
        lines.append(f"[{status}] {name} severity={severity} :: {text}")
    return lines


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return
        for line in summarize(payload):
            print(line, flush=True)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args) -> None:
        pass  # keep the output to alert lines only


if __name__ == "__main__":
    print("alert inbox listening on :8080", flush=True)
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()  # noqa: S104 (inside the container only)
