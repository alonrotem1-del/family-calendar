"""Events the family adds in the app itself (a parents' evening, a birthday).

Kept in Postgres when DATABASE_URL is set (Neon's free plan in production),
otherwise in a local SQLite file. On Neon, DATABASE_URL is the pooled
connection for everyday queries and DATABASE_URL_UNPOOLED the direct one,
which Neon recommends for schema changes like creating the table. That file is fine on a laptop, but on
Render's free plan it is wiped on every restart, so the app warns about it.
"""

import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS family_events (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    people TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    all_day INTEGER NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    first_day TEXT NOT NULL,
    last_day TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

COLUMNS = "id, title, people, start_at, end_at, all_day, location, first_day, last_day, updated_at"


class EventStore:
    def __init__(
        self,
        database_url: str | None = None,
        sqlite_path: Path | None = None,
        direct_url: str | None = None,
    ):
        self.database_url = database_url
        self.sqlite_path = sqlite_path or Path(os.environ.get("SQLITE_PATH") or "family-events.db")
        with self._connect(direct_url) as db:
            db.execute(SCHEMA)

    @property
    def durable(self) -> bool:
        return self.database_url is not None

    @contextmanager
    def _connect(self, url: str | None = None):
        if self.database_url:
            import psycopg  # only needed in production

            # No server-side prepared statements: through Neon's pooler a later
            # request may land on a backend that never saw them.
            with psycopg.connect(url or self.database_url, prepare_threshold=None) as conn:
                yield _Postgres(conn)  # commits on success
        else:
            conn = sqlite3.connect(self.sqlite_path)
            try:
                conn.execute(SCHEMA)  # the local file may have been deleted meanwhile
                yield conn
                conn.commit()
            finally:
                conn.close()

    def between(self, first_day: str, end_day: str) -> list[dict]:
        """Events touching the days [first_day, end_day)."""
        with self._connect() as db:
            rows = db.execute(
                f"SELECT {COLUMNS} FROM family_events WHERE first_day < ? AND last_day >= ?",
                (end_day, first_day),
            ).fetchall()
        return [_row(r) for r in rows]

    def get(self, event_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                f"SELECT {COLUMNS} FROM family_events WHERE id = ?", (event_id,)
            ).fetchone()
        return _row(row) if row else None

    def save(self, event: dict, event_id: str | None = None) -> dict:
        """Adds an event, or replaces the one with `event_id`. Returns None if that one is gone."""
        stored = {
            **event,
            "id": event_id or uuid.uuid4().hex,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        values = (
            stored["title"],
            ",".join(stored["people"]),
            stored["start"],
            stored["end"],
            int(stored["allDay"]),
            stored["location"],
            stored["first_day"],
            stored["last_day"],
            stored["updated_at"],
            stored["id"],
        )
        with self._connect() as db:
            if event_id:
                changed = db.execute(
                    "UPDATE family_events SET title = ?, people = ?, start_at = ?, end_at = ?,"
                    " all_day = ?, location = ?, first_day = ?, last_day = ?, updated_at = ?"
                    " WHERE id = ?",
                    values,
                ).rowcount
                if not changed:
                    return None
            else:
                db.execute(
                    "INSERT INTO family_events (title, people, start_at, end_at, all_day,"
                    " location, first_day, last_day, updated_at, id)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
        return self.get(stored["id"])

    def delete(self, event_id: str) -> bool:
        with self._connect() as db:
            return db.execute("DELETE FROM family_events WHERE id = ?", (event_id,)).rowcount > 0


class _Postgres:
    """Lets the queries above use sqlite's ? placeholders on Postgres too."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql: str, params=()):
        return self._conn.execute(sql.replace("?", "%s"), params)


def _row(row) -> dict:
    (event_id, title, people, start, end, all_day, location, first_day, last_day, updated) = row
    return {
        "id": event_id,
        "title": title,
        "people": [p for p in people.split(",") if p],
        "start": start,
        "end": end,
        "allDay": bool(all_day),
        "location": location,
        "first_day": first_day,
        "last_day": last_day,
        "updated_at": updated,
    }
