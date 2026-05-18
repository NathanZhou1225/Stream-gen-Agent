#!/usr/bin/env python3
"""Newsbox 入库（collectors → MySQL/SQLite）— 供 finance-ingest-cloud Worker 调用。

``finance-ingest-cloud/worker/run_ingest.py`` 动态加载本模块的 ``cmd_run``。
本地 ``ingest.py`` 仅保留 legacy / repair-rsshub，避免 WorkBuddy 误跑本地 SQLite。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from _venv_bootstrap import ensure_venv_and_reexec

    ensure_venv_and_reexec(Path(__file__).resolve())
except ImportError:
    pass

WORKSPACE_ROOT = ROOT.parent.parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _load_dotenv() -> None:
    for path in (
        WORKSPACE_ROOT / ".env",
        WORKSPACE_ROOT.parent / ".env",
        Path("/root/.openclaw/.env"),
        WORKSPACE_ROOT.parent / "finance-ingest-cloud" / ".env",
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


def _resolve_db(db_arg: str) -> Path | str:
    s = (db_arg or "").strip()
    if s.lower() in ("mysql", "cloud"):
        return "mysql"
    if s:
        return Path(s)
    env = os.environ.get("FINANCE_DB_PATH", "").strip()
    if env:
        return Path(env)
    return WORKSPACE_ROOT / "user_data" / "finance_sources.db"


def _all_collectors() -> list[Any]:
    from collectors.cls_telegraph import ClsTelegraphCollector
    from collectors.deep_news import DeepNewsCollector
    from collectors.policy_gov import PolicyGovCollector
    from collectors.rsshub import RSSHubCollector
    from collectors.sina_live import SinaLiveCollector
    from collectors.sina_market import SinaMarketCollector
    from collectors.social_hot import SocialHotCollector

    return [
        SinaMarketCollector(),
        SinaLiveCollector(),
        ClsTelegraphCollector(),
        RSSHubCollector(),
        DeepNewsCollector(),
        PolicyGovCollector(),
        SocialHotCollector(),
    ]


def _run_clean_loop(
    conn: Any,
    storage: Any,
    cleaner: Any,
    max_rounds: int,
) -> tuple[int, int]:
    total_cleaned = 0
    rounds = 0
    while True:
        pending = storage.get_pending_clean_items(conn, batch_size=cleaner.batch_size)
        if not pending:
            break
        cleaned_fields, failed_keys = cleaner.clean_batch(pending)
        if cleaned_fields:
            total_cleaned += storage.apply_cleaned_fields(conn, cleaned_fields)
        if failed_keys:
            storage.mark_clean_failed(conn, failed_keys, model=getattr(cleaner, "_model", ""))
        rounds += 1
        if max_rounds > 0 and rounds >= max_rounds:
            break
    return total_cleaned, rounds


def cmd_run(args: argparse.Namespace) -> None:
    from cleaner import LLMCleaner, max_clean_rounds_per_run
    from models.run import IngestRun
    import storage

    db_path = _resolve_db(getattr(args, "db", "") or "")
    conn = storage.init_db(db_path)

    sources = [s.strip() for s in (args.sources or "market,news,social").split(",") if s.strip()]
    keywords = [k for k in (args.keywords or "").split() if k.strip()]
    prune_days = int(args.prune_days)
    run = IngestRun(
        sources=",".join(sources),
        keywords=" ".join(keywords),
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    collectors = [c for c in _all_collectors() if c.is_enabled(sources)]
    all_news: list[Any] = []
    all_market: list[Any] = []
    all_sentiment: list[Any] = []
    all_errors: list[dict[str, Any]] = []

    if collectors:
        with ThreadPoolExecutor(max_workers=min(len(collectors), 6)) as pool:
            futures = {pool.submit(c._safe_fetch, keywords, args.max_items): c for c in collectors}
            for fut in as_completed(futures):
                result = fut.result()
                all_news.extend(result.news_items)
                all_market.extend(result.market_items)
                all_sentiment.extend(result.sentiment_items)
                all_errors.extend(result.errors)

    inserted, updated = storage.upsert_news(conn, all_news)
    storage.upsert_market(conn, all_market)
    storage.upsert_sentiment(conn, all_sentiment)
    run.inserted = inserted
    run.updated = updated

    clean_rounds = 0
    if not args.no_clean:
        cleaner = LLMCleaner()
        if cleaner.is_available():
            max_r = max_clean_rounds_per_run()
            run.cleaned, clean_rounds = _run_clean_loop(conn, storage, cleaner, max_r)

    run.pruned = storage.prune_old(conn, days=prune_days)
    run.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    run.status = "ok"
    run.errors = all_errors
    storage.save_ingest_run(conn, run)

    result = run.to_summary()
    result["db_path"] = str(db_path)
    result["clean_rounds"] = clean_rounds
    result["pending_clean_remain"] = storage.count_pending_clean(conn)
    if all_errors:
        result["errors"] = all_errors[:10]
    if args.preview:
        recent = storage.query_news(conn, since_hours=2, limit=5)
        result["preview_news"] = [
            {"title": r.get("clean_title") or r.get("raw_title"), "sector": r.get("sector")}
            for r in recent
        ]

    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_init_db(args: argparse.Namespace) -> None:
    import storage

    db_path = _resolve_db(args.db)
    storage.init_db(db_path)
    print(json.dumps({"ok": True, "db_path": str(db_path)}, ensure_ascii=False))


def cmd_prune(args: argparse.Namespace) -> None:
    import storage

    db_path = _resolve_db(args.db)
    conn = storage.init_db(db_path)
    pruned = storage.prune_old(conn, days=int(args.days))
    print(json.dumps({"ok": True, "pruned": pruned, "days": args.days}, ensure_ascii=False))


def cmd_clean(args: argparse.Namespace) -> None:
    from cleaner import LLMCleaner, max_clean_rounds_per_run
    import storage

    db_path = _resolve_db(args.db)
    conn = storage.init_db(db_path)
    cleaner = LLMCleaner()
    if not cleaner.is_available():
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "LLM 清洗未启用或缺少配置",
                    "pending_before": storage.count_pending_clean(conn),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        sys.exit(2)
    max_r = int(args.max_rounds) if args.max_rounds is not None else max_clean_rounds_per_run()
    pending_before = storage.count_pending_clean(conn)
    cleaned, rounds = _run_clean_loop(conn, storage, cleaner, max_r)
    pending_after = storage.count_pending_clean(conn)
    print(
        json.dumps(
            {
                "ok": True,
                "cleaned": cleaned,
                "clean_rounds": rounds,
                "pending_before": pending_before,
                "pending_after": pending_after,
                "db_path": str(db_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="finance-source-ingest Newsbox (cloud worker)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run")
    run_p.add_argument("--sources", default="market,news,social")
    run_p.add_argument("--keywords", default="")
    run_p.add_argument("--max-items", type=int, default=30)
    run_p.add_argument("--db", default="mysql")
    run_p.add_argument("--prune-days", type=int, default=7)
    run_p.add_argument("--no-clean", action="store_true")
    run_p.add_argument("--preview", action="store_true")
    run_p.set_defaults(func=cmd_run)
    init_p = sub.add_parser("init-db")
    init_p.add_argument("--db", default="mysql")
    init_p.set_defaults(func=cmd_init_db)
    prune_p = sub.add_parser("prune")
    prune_p.add_argument("--db", default="mysql")
    prune_p.add_argument("--days", type=int, default=7)
    prune_p.set_defaults(func=cmd_prune)
    clean_p = sub.add_parser("clean")
    clean_p.add_argument("--db", default="mysql")
    clean_p.add_argument("--max-rounds", type=int, default=None)
    clean_p.set_defaults(func=cmd_clean)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
