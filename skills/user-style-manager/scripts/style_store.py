"""SQLite persistence for user style profiles (stored outside skill tree)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from paths import get_db_path, get_user_data_dir


def _connect() -> sqlite3.Connection:
    d = get_user_data_dir()
    d.mkdir(parents=0o700, exist_ok=True)
    p = get_db_path()
    conn = sqlite3.connect(str(p), timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> Path:
    """Create tables if missing. Returns path to DB file."""
    p = get_db_path()
    get_user_data_dir().mkdir(parents=0o700, exist_ok=True)
    with _connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS styles (
                style_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                style_name TEXT NOT NULL,
                style_profile TEXT NOT NULL,
                reference_texts TEXT NOT NULL,
                tags TEXT,
                source_note TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_styles_user ON styles (user_id);
            """
        )
        _migrate_style_columns(c)
        c.commit()
    return p


def _migrate_style_columns(c: sqlite3.Connection) -> None:
    cur = c.execute("PRAGMA table_info(styles)")
    cols = {row[1] for row in cur.fetchall()}
    if "refine_count" not in cols:
        c.execute("ALTER TABLE styles ADD COLUMN refine_count INTEGER NOT NULL DEFAULT 0")
    if "updated_at" not in cols:
        c.execute("ALTER TABLE styles ADD COLUMN updated_at TEXT")


@dataclass
class StyleRow:
    style_id: str
    user_id: str
    style_name: str
    style_profile: dict[str, Any]
    reference_texts: list[str]
    tags: list[str]
    source_note: str | None
    created_at: str
    refine_count: int = 0
    updated_at: str | None = None


def _row_to_style(r: sqlite3.Row) -> StyleRow:
    keys = r.keys()
    return StyleRow(
        style_id=r["style_id"],
        user_id=r["user_id"],
        style_name=r["style_name"],
        style_profile=json.loads(r["style_profile"]),
        reference_texts=json.loads(r["reference_texts"]),
        tags=json.loads(r["tags"] or "[]"),
        source_note=r["source_note"],
        created_at=r["created_at"],
        refine_count=int(
            (r["refine_count"] if "refine_count" in keys else None) or 0
        ),
        updated_at=r["updated_at"] if "updated_at" in keys else None,
    )


def insert_style(
    user_id: str,
    style_name: str,
    style_profile: dict[str, Any],
    reference_texts: list[str],
    tags: list[str] | None = None,
    source_note: str | None = None,
    style_id: str | None = None,
) -> str:
    sid = style_id or str(uuid.uuid4())
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    tags = tags or []
    with _connect() as c:
        c.execute(
            """
            INSERT INTO styles (style_id, user_id, style_name, style_profile, reference_texts, tags, source_note, created_at, refine_count, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
            """,
            (
                sid,
                user_id,
                style_name,
                json.dumps(style_profile, ensure_ascii=False),
                json.dumps(reference_texts, ensure_ascii=False),
                json.dumps(tags, ensure_ascii=False),
                source_note,
                now,
            ),
        )
        c.commit()
    return sid


def update_style_row(
    user_id: str,
    style_id: str,
    *,
    style_name: str,
    style_profile: dict[str, Any],
    reference_texts: list[str],
    tags: list[str] | None,
    source_note: str | None,
    increment_refine: bool = True,
) -> bool:
    init_db()
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    tags = tags if tags is not None else []
    with _connect() as c:
        r = c.execute(
            "SELECT 1 FROM styles WHERE style_id = ? AND user_id = ?",
            (style_id, user_id),
        ).fetchone()
        if not r:
            return False
        if increment_refine:
            c.execute(
                """
                UPDATE styles SET
                    style_name = ?,
                    style_profile = ?,
                    reference_texts = ?,
                    tags = ?,
                    source_note = ?,
                    refine_count = COALESCE(refine_count, 0) + 1,
                    updated_at = ?
                WHERE style_id = ? AND user_id = ?
                """,
                (
                    style_name,
                    json.dumps(style_profile, ensure_ascii=False),
                    json.dumps(reference_texts, ensure_ascii=False),
                    json.dumps(tags, ensure_ascii=False),
                    source_note,
                    now,
                    style_id,
                    user_id,
                ),
            )
        else:
            c.execute(
                """
                UPDATE styles SET
                    style_name = ?,
                    style_profile = ?,
                    reference_texts = ?,
                    tags = ?,
                    source_note = ?,
                    updated_at = ?
                WHERE style_id = ? AND user_id = ?
                """,
                (
                    style_name,
                    json.dumps(style_profile, ensure_ascii=False),
                    json.dumps(reference_texts, ensure_ascii=False),
                    json.dumps(tags, ensure_ascii=False),
                    source_note,
                    now,
                    style_id,
                    user_id,
                ),
            )
        c.commit()
    return True


def list_styles(user_id: str) -> list[StyleRow]:
    init_db()
    with _connect() as c:
        cur = c.execute(
            "SELECT * FROM styles WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        )
        return [_row_to_style(r) for r in cur.fetchall()]


def get_style(style_id: str) -> StyleRow | None:
    init_db()
    with _connect() as c:
        cur = c.execute("SELECT * FROM styles WHERE style_id = ?", (style_id,))
        r = cur.fetchone()
        return _row_to_style(r) if r else None


def get_style_for_user(user_id: str, style_id: str) -> StyleRow | None:
    s = get_style(style_id)
    if s is None or s.user_id != user_id:
        return None
    return s


def delete_style(user_id: str, style_id: str) -> bool:
    init_db()
    with _connect() as c:
        cur = c.execute(
            "DELETE FROM styles WHERE style_id = ? AND user_id = ?",
            (style_id, user_id),
        )
        c.commit()
        return cur.rowcount > 0
