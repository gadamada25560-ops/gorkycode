from __future__ import annotations

import csv
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
ON_VERCEL = bool(os.getenv("VERCEL"))
if ON_VERCEL:
    DB_PATH = Path("/tmp/assistant.db")
else:
    DB_PATH = ROOT / "data" / "assistant.db"


def csv_path() -> Path:
    candidates = (
        ROOT / "data" / "voice_assistant_events.csv",
        Path(__file__).resolve().parent / "voice_assistant_events.csv",
        Path.cwd() / "data" / "voice_assistant_events.csv",
    )
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


MEETING_MARKERS = ("встреч", "созвон", "демо", "планёр", "планер")
REMINDER_MARKERS = ("напомн",)


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE" if ON_VERCEL else "PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            date TEXT,
            time TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'user'
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    if "description" not in columns:
        conn.execute("ALTER TABLE events ADD COLUMN description TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_date ON events(date, time)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_title ON events(title)"
    )
    conn.commit()
    seed_from_csv(conn)
    fill_missing_descriptions(conn)


def classify_title(title: str) -> str:
    low = title.lower()
    if any(marker in low for marker in MEETING_MARKERS):
        return "meeting"
    if any(marker in low for marker in REMINDER_MARKERS):
        return "reminder"
    return "task"


def build_description(event_type: str, title: str, date: str | None, time: str | None) -> str:
    kind = {"task": "Задача", "meeting": "Встреча", "reminder": "Напоминание"}.get(event_type, "Запись")
    bits = [f"{kind} «{title}»"]
    if date and time:
        bits.append(f"на {date} в {time}")
    elif date:
        bits.append(f"на {date}")
    elif time:
        bits.append(f"в {time}")
    else:
        bits.append("без точной даты")
    return " ".join(bits) + "."


def fill_missing_descriptions(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT id, event_type, title, date, time FROM events WHERE description IS NULL OR description = ''"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE events SET description = ? WHERE id = ?",
            (build_description(row["event_type"], row["title"], row["date"], row["time"]), row["id"]),
        )
    if rows:
        conn.commit()


def seed_from_csv(conn: sqlite3.Connection) -> None:
    count = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    path = csv_path()
    if count or not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for row in reader:
            title = (row.get("text") or "").strip()
            if not title:
                continue
            date = (row.get("date") or "").strip() or None
            time = (row.get("time") or "").strip() or None
            created = f"{date}T{time}:00" if date and time else datetime.now().isoformat(timespec="seconds")
            event_type = classify_title(title)
            rows.append(
                (
                    event_type,
                    title,
                    build_description(event_type, title, date, time),
                    date,
                    time,
                    "open",
                    created,
                    "seed",
                )
            )
        conn.executemany(
            """
            INSERT INTO events (event_type, title, description, date, time, status, created_at, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def create_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    title: str,
    date: str | None,
    time: str | None,
    description: str | None = None,
) -> dict[str, Any]:
    created_at = datetime.now().isoformat(timespec="seconds")
    title = title.strip()
    description = (description or "").strip() or build_description(event_type, title, date, time)
    cur = conn.execute(
        """
        INSERT INTO events (event_type, title, description, date, time, status, created_at, source)
        VALUES (?, ?, ?, ?, ?, 'open', ?, 'user')
        """,
        (event_type, title, description, date, time, created_at),
    )
    conn.commit()
    return get_event(conn, cur.lastrowid)


def get_event(conn: sqlite3.Connection, event_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return row_to_dict(row) if row else None


def update_event(conn: sqlite3.Connection, event_id: int, **fields: Any) -> dict[str, Any] | None:
    allowed = {"event_type", "title", "description", "date", "time", "status"}
    updates = {key: value for key, value in fields.items() if key in allowed and value is not None}
    if not updates:
        return get_event(conn, event_id)
    assignments = ", ".join(f"{key} = ?" for key in updates)
    conn.execute(
        f"UPDATE events SET {assignments} WHERE id = ?",
        [*updates.values(), event_id],
    )
    conn.commit()
    return get_event(conn, event_id)


def delete_event(conn: sqlite3.Connection, event_id: int) -> bool:
    cur = conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    conn.commit()
    return cur.rowcount > 0


def list_events(
    conn: sqlite3.Connection,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = "open",
    limit: int = 80,
) -> list[dict[str, Any]]:
    clauses = ["1=1"]
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if date_from:
        clauses.append("date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("date <= ?")
        params.append(date_to)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT * FROM events
        WHERE {' AND '.join(clauses)}
        ORDER BY date IS NULL, date, time IS NULL, time, id
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [row_to_dict(row) for row in rows]


def _search_tokens(query: str) -> list[str]:
    raw = [token.strip(".,!?«»:;").lower() for token in query.replace(",", " ").split()]
    tokens = []
    for token in raw:
        if len(token) < 4:
            continue
        if token in {"найди", "найти", "задачу", "задача", "задачи", "встреч", "встреча", "когда", "меня", "было", "про", "для"}:
            continue
        stem = token[:6] if len(token) > 6 else token
        tokens.append(stem)
    return tokens or [query.strip()]


def search_events(conn: sqlite3.Connection, query: str, limit: int = 25) -> list[dict[str, Any]]:
    tokens = _search_tokens(query)
    like_clauses = " OR ".join(["lower(title) LIKE ?"] * len(tokens))
    today = datetime.now().strftime("%Y-%m-%d")
    params = [f"%{token}%" for token in tokens]
    params.extend([today, limit])
    rows = conn.execute(
        f"""
        SELECT * FROM events
        WHERE {like_clauses}
        ORDER BY CASE WHEN date >= ? THEN 0 ELSE 1 END, date IS NULL, date, time IS NULL, time, id
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [row_to_dict(row) for row in rows]


def find_conflicts(conn: sqlite3.Connection, date: str, time: str, exclude_id: int | None = None) -> list[dict[str, Any]]:
    if not date or not time:
        return []
    if exclude_id is None:
        rows = conn.execute(
            """
            SELECT * FROM events
            WHERE event_type = 'meeting' AND status = 'open' AND date = ? AND time = ?
            """,
            (date, time),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM events
            WHERE event_type = 'meeting' AND status = 'open' AND date = ? AND time = ? AND id != ?
            """,
            (date, time, exclude_id),
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def upcoming(conn: sqlite3.Connection, today: str, limit: int = 40) -> list[dict[str, Any]]:
    return list_events(conn, date_from=today, status="open", limit=limit)
