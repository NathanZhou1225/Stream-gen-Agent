#!/usr/bin/env python3
"""
finance-draft-manager — 数据库检索器。

职责：从 finance-source-ingest 产生的 SQLite 中检索相关素材，
     输出 source_context / evidence_pack，供 streamy-content-gen 的
     preflight_topic 与 draft_manager 开稿门禁使用。

不联网、不抓取、不调用 pipeline.py。

用法：
  draft_retriever.py retrieve --direction "AI算力" --since-hours 24 --limit 10
  draft_retriever.py build-context --direction "AI算力" [--since-hours 24] [--db PATH]
  draft_retriever.py status [--db PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
WORKSPACE_ROOT = SKILL_ROOT.parent.parent

# 优先引用 finance-source-ingest 的 storage.py
INGEST_ROOT = WORKSPACE_ROOT / "skills" / "finance-source-ingest"
if str(INGEST_ROOT) not in sys.path:
    sys.path.insert(0, str(INGEST_ROOT))


def _load_dotenv() -> None:
    for path in (
        WORKSPACE_ROOT / ".env",
        WORKSPACE_ROOT.parent / ".env",
        Path("/root/.openclaw/.env"),
    ):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()


def _resolve_db(db_arg: str = "") -> Path:
    if db_arg:
        return Path(db_arg)
    env = os.environ.get("FINANCE_DB_PATH", "").strip()
    if env:
        return Path(env)
    return WORKSPACE_ROOT / "user_data" / "finance_sources.db"


def _connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(
            f"数据库文件不存在：{db_path}。请先运行 finance-source-ingest/scripts/ingest.py run"
        )
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _direction_to_keywords(direction: str) -> list[str]:
    """将自然语言方向拆为检索关键词。"""
    import re
    raw = (direction or "").strip()
    if not raw:
        return []
    s = re.sub(r'[，。、；;:!！?？"""\'\'（）()\[\]【】\s]+', " ", raw)
    parts = [p.strip() for p in s.split() if len(p.strip()) >= 2]
    return parts[:8]


def _retrieve(
    conn: sqlite3.Connection,
    keywords: list[str],
    since_hours: int,
    limit: int,
    sector: str = "",
) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat(timespec="seconds")
    clauses = ["fetched_at >= ?"]
    params: list = [cutoff]

    if sector:
        clauses.append("sector = ?")
        params.append(sector)

    if keywords:
        kw_or = " OR ".join(
            ["(raw_title LIKE ? OR clean_title LIKE ? OR clean_summary LIKE ?)"] * len(keywords)
        )
        clauses.append(f"({kw_or})")
        for kw in keywords:
            params.extend([f"%{kw}%", f"%{kw}%", f"%{kw}%"])

    where = " AND ".join(clauses)
    sql = f"""
        SELECT dedupe_key, source, source_url,
               raw_title, clean_title, clean_summary,
               sector, sentiment, importance_score, tags_json,
               published_at, fetched_at
        FROM news_items
        WHERE {where}
        ORDER BY importance_score DESC, fetched_at DESC
        LIMIT ?
    """
    params.append(limit)
    cur = conn.execute(sql, params)
    return [dict(row) for row in cur.fetchall()]


def _market_latest(conn: sqlite3.Connection, since_hours: int = 4) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat(timespec="seconds")
    cur = conn.execute(
        "SELECT * FROM market_snapshots WHERE snapshot_at >= ? ORDER BY snapshot_at DESC LIMIT 20",
        (cutoff,),
    )
    return [dict(row) for row in cur.fetchall()]


def cmd_retrieve(args: argparse.Namespace) -> None:
    db_path = _resolve_db(args.db)
    try:
        conn = _connect(db_path)
    except FileNotFoundError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2))
        return

    keywords = _direction_to_keywords(args.direction)
    rows = _retrieve(conn, keywords, since_hours=args.since_hours, limit=args.limit, sector=args.sector)
    print(json.dumps({"ok": True, "count": len(rows), "items": rows}, ensure_ascii=False, indent=2))


def cmd_build_context(args: argparse.Namespace) -> None:
    """
    从 DB 检索 → 组装 source_context + evidence_pack。
    输出格式与 preflight_topic.py 兼容，可直接传给 draft_manager --set-evidence-pack-file。
    """
    db_path = _resolve_db(args.db)
    try:
        conn = _connect(db_path)
    except FileNotFoundError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2))
        return

    direction = args.direction or ""
    keywords = _direction_to_keywords(direction)
    rows = _retrieve(conn, keywords, since_hours=args.since_hours, limit=10)
    market = _market_latest(conn)

    # source_context bullets（≤8 条）
    bullets: list[str] = []
    for r in rows[:8]:
        title = r.get("clean_title") or r.get("raw_title") or ""
        summary = r.get("clean_summary") or ""
        sector = r.get("sector") or ""
        src = r.get("source") or ""
        line = f"- [{sector}|{src}] {title}"
        if summary:
            line += f"：{summary[:80]}"
        bullets.append(line)

    # evidence_pack（取前 5 条作详情）
    evidence_rows = []
    for r in rows[:5]:
        evidence_rows.append({
            "point": (r.get("clean_title") or r.get("raw_title") or "")[:80],
            "source_type": r.get("source") or "db",
            "source_ref": r.get("source_url") or r.get("fetched_at") or "",
            "confidence": "high" if r.get("llm_clean_status") == "done" else "medium",
            "sector": r.get("sector") or "",
            "sentiment": r.get("sentiment") or "",
        })

    result = {
        "ok": True,
        "direction": direction,
        "db_path": str(db_path),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_context": bullets,
        "evidence_pack": {
            "direction": direction,
            "core_facts": evidence_rows,
            "market_snapshot": market[:6],
            "source_gaps": [] if len(rows) >= 3 else ["DB 中匹配该方向的条目不足 3 条，建议先运行 ingest.py run 更新数据"],
            "usage_hint": "先向用户展示本 evidence_pack；用户确认后再进入 user-style 选择/绑定。不得跳过证据包直接生成大纲。",
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_status(args: argparse.Namespace) -> None:
    """展示 DB 健康状态：各表条数、最新入库时间、待清洗条数。"""
    db_path = _resolve_db(args.db)
    try:
        conn = _connect(db_path)
    except FileNotFoundError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2))
        return

    def _count(table: str, where: str = "1=1") -> int:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]

    last_fetch = conn.execute(
        "SELECT fetched_at FROM news_items ORDER BY fetched_at DESC LIMIT 1"
    ).fetchone()

    status = {
        "ok": True,
        "db_path": str(db_path),
        "news_items_total": _count("news_items"),
        "news_items_pending_clean": _count("news_items", "llm_clean_status='pending'"),
        "news_items_clean_done": _count("news_items", "llm_clean_status='done'"),
        "news_items_clean_failed": _count("news_items", "llm_clean_status='failed'"),
        "market_snapshots_total": _count("market_snapshots"),
        "sentiment_hot_total": _count("sentiment_hot"),
        "ingest_runs_total": _count("ingest_runs"),
        "last_fetched_at": last_fetch[0] if last_fetch else None,
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="finance-draft-manager — DB 检索与开稿上下文构建")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ret_p = sub.add_parser("retrieve", help="按方向关键词检索 news_items")
    ret_p.add_argument("--direction", default="", help="开稿方向（自然语言）")
    ret_p.add_argument("--since-hours", type=int, default=24)
    ret_p.add_argument("--limit", type=int, default=10)
    ret_p.add_argument("--sector", default="")
    ret_p.add_argument("--db", default="")
    ret_p.set_defaults(func=cmd_retrieve)

    ctx_p = sub.add_parser("build-context", help="构建 source_context + evidence_pack（供 streamy-content-gen 使用）")
    ctx_p.add_argument("--direction", required=True)
    ctx_p.add_argument("--since-hours", type=int, default=24)
    ctx_p.add_argument("--db", default="")
    ctx_p.set_defaults(func=cmd_build_context)

    st_p = sub.add_parser("status", help="展示 DB 健康状态")
    st_p.add_argument("--db", default="")
    st_p.set_defaults(func=cmd_status)

    args = parser.parse_args()
    fn = getattr(args, "func", None)
    if fn:
        fn(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
