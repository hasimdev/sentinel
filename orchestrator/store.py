"""Incident storage in a local SQLite file, so incidents survive restarts."""

import sqlite3
from contextlib import closing
from pathlib import Path

from orchestrator.models import Incident

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incidents_open ON incidents (fingerprint, status);
"""


class IncidentStore:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def create(self, incident: Incident) -> Incident:
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO incidents (fingerprint, status, body) VALUES (?, ?, ?)",
                (incident.fingerprint, incident.status, ""),
            )
            created = incident.model_copy(update={"id": cur.lastrowid})
            conn.execute(
                "UPDATE incidents SET body = ? WHERE id = ?",
                (created.model_dump_json(), created.id),
            )
        return created

    def save(self, incident: Incident) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE incidents SET status = ?, body = ? WHERE id = ?",
                (incident.status, incident.model_dump_json(), incident.id),
            )

    def get(self, incident_id: int) -> Incident | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT body FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        return Incident.model_validate_json(row[0]) if row else None

    def find_open(self, fingerprint: str) -> Incident | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT body FROM incidents WHERE fingerprint = ? AND status = 'open' "
                "ORDER BY id DESC LIMIT 1",
                (fingerprint,),
            ).fetchone()
        return Incident.model_validate_json(row[0]) if row else None

    def list(self) -> list[Incident]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT body FROM incidents ORDER BY id DESC").fetchall()
        return [Incident.model_validate_json(row[0]) for row in rows]
