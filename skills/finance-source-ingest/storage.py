"""SQLite 存储层：init / upsert / prune / query。

设计原则（参考 newsbox）：
- 采集层只负责 raw 入库和 normalized 字段更新，不做内容判断。
- dedupe_key 唯一约束：相同 URL/标题+时间的条目执行 UPSERT（更新 fetched_at 与 raw_payload）。
- prune_old 默认清理 7 天前数据，每次 ingest run 末尾自动调用。
- 所有写操作使用事务，失败整体回滚。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator

from models.item import CleanedFields, RawNewsItem
from models.market import MarketSnapshot
from models.run import IngestRun
from models.sentiment import SentimentHotItem

logger = logging.getLogger(__name__)

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS ingest_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    sources     TEXT,
    keywords    TEXT,
    status      TEXT DEFAULT 'running',
    inserted    INTEGER DEFAULT 0,
    updated     INTEGER DEFAULT 0,
    cleaned     INTEGER DEFAULT 0,
    pruned      INTEGER DEFAULT 0,
    error_json  TEXT
);

CREATE TABLE IF NOT EXISTS news_items (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key         TEXT UNIQUE NOT NULL,
    source             TEXT NOT NULL,
    source_url         TEXT,
    raw_title          TEXT,
    raw_content        TEXT,
    raw_payload_json   TEXT,
    clean_title        TEXT,
    clean_summary      TEXT,
    sector             TEXT,
    sentiment          TEXT,
    importance_score   REAL DEFAULT 0.0,
    tags_json          TEXT,
    published_at       TEXT,
    fetched_at         TEXT NOT NULL,
    llm_clean_status   TEXT DEFAULT 'pending',
    llm_clean_model    TEXT,
    llm_cleaned_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_news_fetched_at ON news_items(fetched_at);
CREATE INDEX IF NOT EXISTS idx_news_sector     ON news_items(sector);
CREATE INDEX IF NOT EXISTS idx_news_source     ON news_items(source);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_at      TEXT NOT NULL,
    index_code       TEXT NOT NULL,
    index_name       TEXT,
    price            REAL,
    change_pct       REAL,
    volume           REAL,
    turnover         REAL,
    raw_payload_json TEXT,
    UNIQUE(snapshot_at, index_code)
);

CREATE INDEX IF NOT EXISTS idx_market_snapshot_at ON market_snapshots(snapshot_at);

CREATE TABLE IF NOT EXISTS sentiment_hot (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_at      TEXT NOT NULL,
    source           TEXT NOT NULL,
    keyword          TEXT,
    rank             INTEGER,
    heat             TEXT,
    sector           TEXT,
    related_stock    TEXT,
    raw_payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_sentiment_snapshot_at ON sentiment_hot(snapshot_at);

CREATE TABLE IF NOT EXISTS social_intel_run_history (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at          TEXT NOT NULL,
    headline_sentiment   REAL NOT NULL,
    mean_buzz_score      REAL NOT NULL,
    fear_greed_index     REAL NOT NULL,
    dedupe_unique_count  INTEGER,
    source_kind          TEXT NOT NULL DEFAULT 'legacy_pipeline'
);

CREATE INDEX IF NOT EXISTS idx_social_intel_hist_time ON social_intel_run_history(recorded_at);

CREATE TABLE IF NOT EXISTS source_state (
    source_name     TEXT PRIMARY KEY,
    last_fetched_at TEXT,
    last_ok_at      TEXT,
    fail_streak     INTEGER DEFAULT 0,
    total_fetched   INTEGER DEFAULT 0
);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path) -> sqlite3.Connection:
    """初始化数据库，建表（幂等）。返回连接。"""
    conn = _connect(db_path)
    conn.executescript(_DDL)
    conn.commit()
    logger.info("DB initialized: %s", db_path)
    return conn


# ── upsert helpers ────────────────────────────────────────────────────────────

def upsert_news(conn: sqlite3.Connection, items: list[RawNewsItem]) -> tuple[int, int]:
    """
    批量 upsert news_items。返回 (inserted, updated)。
    已存在（dedupe_key 冲突）时更新 fetched_at 与 raw_payload_json。
    """
    inserted = updated = 0
    with conn:
        for item in items:
            cur = conn.execute("SELECT id FROM news_items WHERE dedupe_key = ?", (item.dedupe_key,))
            row = cur.fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO news_items
                       (dedupe_key, source, source_url, raw_title, raw_content,
                        raw_payload_json, published_at, fetched_at, llm_clean_status)
                       VALUES (?,?,?,?,?,?,?,?,'pending')""",
                    (
                        item.dedupe_key,
                        item.source,
                        item.source_url,
                        item.raw_title,
                        item.raw_content,
                        item.raw_payload_json(),
                        item.published_at,
                        item.fetched_at,
                    ),
                )
                inserted += 1
            else:
                conn.execute(
                    """UPDATE news_items
                       SET fetched_at=?, raw_payload_json=?
                       WHERE dedupe_key=?""",
                    (item.fetched_at, item.raw_payload_json(), item.dedupe_key),
                )
                updated += 1
    return inserted, updated


def upsert_market(conn: sqlite3.Connection, items: list[MarketSnapshot]) -> int:
    """批量 upsert market_snapshots。返回写入行数。"""
    count = 0
    with conn:
        for item in items:
            conn.execute(
                """INSERT INTO market_snapshots
                   (snapshot_at, index_code, index_name, price, change_pct, volume, turnover, raw_payload_json)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(snapshot_at, index_code) DO UPDATE
                   SET price=excluded.price, change_pct=excluded.change_pct,
                       volume=excluded.volume, turnover=excluded.turnover,
                       raw_payload_json=excluded.raw_payload_json""",
                (
                    item.snapshot_at,
                    item.index_code,
                    item.index_name,
                    item.price,
                    item.change_pct,
                    item.volume,
                    item.turnover,
                    item.raw_payload_json(),
                ),
            )
            count += 1
    return count


def upsert_sentiment(conn: sqlite3.Connection, items: list[SentimentHotItem]) -> int:
    """批量插入 sentiment_hot（每次快照插入，不去重）。返回写入行数。"""
    count = 0
    with conn:
        for item in items:
            conn.execute(
                """INSERT INTO sentiment_hot
                   (snapshot_at, source, keyword, rank, heat, sector, related_stock, raw_payload_json)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    item.snapshot_at,
                    item.source,
                    item.keyword,
                    item.rank,
                    item.heat,
                    item.sector,
                    item.related_stock,
                    item.raw_payload_json(),
                ),
            )
            count += 1
    return count


def apply_cleaned_fields(conn: sqlite3.Connection, fields_list: list[CleanedFields]) -> int:
    """将 LLM 清洗结果写回 news_items 的 normalized 字段。"""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    count = 0
    with conn:
        for f in fields_list:
            conn.execute(
                """UPDATE news_items
                   SET clean_title=?, clean_summary=?, sector=?, sentiment=?,
                       importance_score=?, tags_json=?,
                       llm_clean_status='done', llm_clean_model=?, llm_cleaned_at=?
                   WHERE dedupe_key=?""",
                (
                    f.clean_title,
                    f.clean_summary,
                    f.sector,
                    f.sentiment,
                    f.importance_score,
                    f.tags_json(),
                    f.llm_clean_model,
                    now,
                    f.dedupe_key,
                ),
            )
            count += 1
    return count


def mark_clean_failed(conn: sqlite3.Connection, dedupe_keys: list[str], model: str = "") -> None:
    """将清洗失败的条目标记为 failed，raw 数据保留。"""
    with conn:
        for key in dedupe_keys:
            conn.execute(
                "UPDATE news_items SET llm_clean_status='failed', llm_clean_model=? WHERE dedupe_key=?",
                (model, key),
            )


# ── prune ─────────────────────────────────────────────────────────────────────

def prune_old(conn: sqlite3.Connection, days: int = 7) -> int:
    """
    清理 days 天前的历史数据。
    返回删除行数（news_items + market_snapshots + sentiment_hot + social_intel_run_history 合计）。
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).isoformat(timespec="seconds")
    total = 0
    with conn:
        cur = conn.execute("DELETE FROM news_items WHERE fetched_at < ?", (cutoff,))
        total += cur.rowcount
        cur = conn.execute("DELETE FROM market_snapshots WHERE snapshot_at < ?", (cutoff,))
        total += cur.rowcount
        cur = conn.execute("DELETE FROM sentiment_hot WHERE snapshot_at < ?", (cutoff,))
        total += cur.rowcount
        cur = conn.execute("DELETE FROM social_intel_run_history WHERE recorded_at < ?", (cutoff,))
        total += cur.rowcount
    logger.info("Pruned %d rows older than %d days (cutoff=%s)", total, days, cutoff)
    return total


# ── query helpers ─────────────────────────────────────────────────────────────

def query_news(
    conn: sqlite3.Connection,
    *,
    sector: str = "",
    direction_keywords: list[str] | None = None,
    since_hours: int = 24,
    limit: int = 20,
    min_importance: float = 0.0,
    clean_only: bool = False,
) -> list[dict[str, Any]]:
    """
    从 news_items 检索最近数据，供 finance-draft-manager 使用。
    clean_only=True 时只返回已 LLM 清洗的条目。
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=since_hours)
    ).isoformat(timespec="seconds")

    clauses = ["fetched_at >= ?"]
    params: list[Any] = [cutoff]

    if sector:
        clauses.append("sector = ?")
        params.append(sector)

    if min_importance > 0:
        clauses.append("importance_score >= ?")
        params.append(min_importance)

    if clean_only:
        clauses.append("llm_clean_status = 'done'")

    if direction_keywords:
        kw_clauses = " OR ".join(
            ["(raw_title LIKE ? OR clean_title LIKE ? OR clean_summary LIKE ?)"] * len(direction_keywords)
        )
        clauses.append(f"({kw_clauses})")
        for kw in direction_keywords:
            params.extend([f"%{kw}%", f"%{kw}%", f"%{kw}%"])

    where = " AND ".join(clauses)
    sql = f"""
        SELECT id, dedupe_key, source, source_url,
               raw_title, clean_title, clean_summary,
               sector, sentiment, importance_score, tags_json,
               published_at, fetched_at, llm_clean_status
        FROM news_items
        WHERE {where}
        ORDER BY importance_score DESC, fetched_at DESC
        LIMIT ?
    """
    params.append(limit)
    cur = conn.execute(sql, params)
    return [dict(row) for row in cur.fetchall()]


def query_news_since_fetched_at(
    conn: sqlite3.Connection,
    *,
    since_iso: str,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """``fetched_at >= since_iso`` 的新闻行，按入库新→旧，供 ingest run 末尾社交情报聚合。"""
    cur = conn.execute(
        """SELECT id, dedupe_key, source, source_url,
               raw_title, clean_title, clean_summary,
               sector, sentiment, importance_score, tags_json,
               published_at, fetched_at, llm_clean_status
           FROM news_items
           WHERE fetched_at >= ?
           ORDER BY fetched_at DESC
           LIMIT ?""",
        (since_iso, limit),
    )
    return [dict(row) for row in cur.fetchall()]


def query_market_latest(
    conn: sqlite3.Connection,
    since_hours: int = 24,
) -> list[dict[str, Any]]:
    """返回最近一次行情快照的各指数条目。"""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=since_hours)
    ).isoformat(timespec="seconds")
    cur = conn.execute(
        """SELECT * FROM market_snapshots
           WHERE snapshot_at >= ?
           ORDER BY snapshot_at DESC""",
        (cutoff,),
    )
    return [dict(row) for row in cur.fetchall()]


def save_ingest_run(conn: sqlite3.Connection, run: IngestRun) -> int:
    """写入 ingest_runs 记录，返回 rowid。"""
    with conn:
        cur = conn.execute(
            """INSERT INTO ingest_runs
               (started_at, finished_at, sources, keywords, status, inserted, updated, cleaned, pruned, error_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                run.started_at,
                run.finished_at,
                run.sources,
                run.keywords,
                run.status,
                run.inserted,
                run.updated,
                run.cleaned,
                run.pruned,
                json.dumps(run.errors, ensure_ascii=False) if run.errors else None,
            ),
        )
    return cur.lastrowid or 0


def get_pending_clean_items(
    conn: sqlite3.Connection,
    batch_size: int = 10,
) -> list[dict[str, Any]]:
    """返回待清洗（llm_clean_status='pending'）的条目。"""
    cur = conn.execute(
        """SELECT dedupe_key, source, raw_title, raw_content, published_at
           FROM news_items
           WHERE llm_clean_status = 'pending'
           ORDER BY fetched_at DESC
           LIMIT ?""",
        (batch_size,),
    )
    return [dict(row) for row in cur.fetchall()]


def count_pending_clean(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "SELECT COUNT(*) FROM news_items WHERE llm_clean_status = 'pending'",
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0


def reset_failed_clean_to_pending(conn: sqlite3.Connection) -> int:
    """将清洗失败的条目改回 pending，便于修配置后重洗（raw 保留）。"""
    with conn:
        cur = conn.execute(
            """UPDATE news_items
               SET llm_clean_status='pending',
                   llm_clean_model=NULL,
                   llm_cleaned_at=NULL
               WHERE llm_clean_status='failed'""",
        )
    return cur.rowcount


def ensure_social_intel_history_schema(conn: sqlite3.Connection) -> None:
    """幂等：旧库缺表时补建 ``social_intel_run_history``。"""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS social_intel_run_history (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at          TEXT NOT NULL,
            headline_sentiment   REAL NOT NULL,
            mean_buzz_score      REAL NOT NULL,
            fear_greed_index     REAL NOT NULL,
            dedupe_unique_count  INTEGER,
            source_kind          TEXT NOT NULL DEFAULT 'legacy_pipeline'
        )""",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_social_intel_hist_time ON social_intel_run_history(recorded_at)",
    )
    conn.commit()


_DEFAULT_SOCIAL_INTEL_SOURCE_KINDS: tuple[str, ...] = ("legacy_pipeline", "ingest_run")


def fetch_social_intel_run_history(
    conn: sqlite3.Connection,
    limit: int,
    *,
    source_kinds: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """取最近 ``limit`` 条记录，按时间**升序**（旧→新）返回，供 FG / 反转序列拼接。

    ``source_kinds`` 默认 ``("legacy_pipeline", "ingest_run")``，与定时入库写入的历史合并；
    传空元组等价于不按 ``source_kind`` 过滤（慎用）。
    """
    if limit <= 0:
        return []
    ensure_social_intel_history_schema(conn)
    if source_kinds is not None and len(source_kinds) == 0:
        cur = conn.execute(
            """SELECT recorded_at, headline_sentiment, mean_buzz_score, fear_greed_index, dedupe_unique_count, source_kind
               FROM social_intel_run_history
               ORDER BY recorded_at DESC, id DESC
               LIMIT ?""",
            (limit,),
        )
    elif source_kinds is not None:
        ph = ",".join("?" * len(source_kinds))
        cur = conn.execute(
            f"""SELECT recorded_at, headline_sentiment, mean_buzz_score, fear_greed_index, dedupe_unique_count, source_kind
               FROM social_intel_run_history
               WHERE source_kind IN ({ph})
               ORDER BY recorded_at DESC, id DESC
               LIMIT ?""",
            (*source_kinds, limit),
        )
    else:
        kinds = _DEFAULT_SOCIAL_INTEL_SOURCE_KINDS
        ph = ",".join("?" * len(kinds))
        cur = conn.execute(
            f"""SELECT recorded_at, headline_sentiment, mean_buzz_score, fear_greed_index, dedupe_unique_count, source_kind
               FROM social_intel_run_history
               WHERE source_kind IN ({ph})
               ORDER BY recorded_at DESC, id DESC
               LIMIT ?""",
            (*kinds, limit),
        )
    rows = [dict(row) for row in cur.fetchall()]
    rows.reverse()
    return rows


def append_social_intel_run_history(
    conn: sqlite3.Connection,
    *,
    recorded_at: str,
    headline_sentiment: float,
    mean_buzz_score: float,
    fear_greed_index: float,
    dedupe_unique_count: int,
    source_kind: str = "legacy_pipeline",
) -> None:
    """追加一次快照级社交情报聚合（供下一跑读历史）。"""
    ensure_social_intel_history_schema(conn)
    with conn:
        conn.execute(
            """INSERT INTO social_intel_run_history
               (recorded_at, headline_sentiment, mean_buzz_score, fear_greed_index, dedupe_unique_count, source_kind)
               VALUES (?,?,?,?,?,?)""",
            (
                recorded_at,
                headline_sentiment,
                mean_buzz_score,
                fear_greed_index,
                dedupe_unique_count,
                source_kind,
            ),
        )
