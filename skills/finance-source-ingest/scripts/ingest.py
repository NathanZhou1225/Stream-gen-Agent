#!/usr/bin/env python3
"""finance-source-ingest — Finance Newsbox 主入口。

v0.2.2 架构：采集器（collectors/）并发抓取 → raw 入库 → LLM 清洗 → prune → 极简 JSON stdout。

用法：
  ingest.py run [--sources market,news,social] [--keywords "AI 算力"] [--max-items 30]
               [--db PATH] [--prune-days 7] [--no-clean] [--preview]
  ingest.py clean [--db PATH] [--max-rounds N]  # 仅清洗 pending，不抓取
  ingest.py init-db [--db PATH]
  ingest.py prune [--db PATH] [--days 7]
  ingest.py repair-rsshub [--decision confirm|ignore] [--token TOKEN]
  ingest.py legacy [--sources ...] [--max-items ...] [--out-dir ...]

  legacy 子命令：原 pipeline.py build_snapshot 路径（向后兼容，供 query_market_facts.py 调用）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── 路径引导 ─────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent  # finance-source-ingest/
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 首次用系统 python 时自动建 .venv（可 FINANCE_INGEST_NO_AUTO_VENV=1 关闭）
try:
    from _venv_bootstrap import ensure_venv_and_reexec
    ensure_venv_and_reexec(Path(__file__).resolve())
except ImportError:
    pass  # 已在 venv 内或关闭了 bootstrap

WORKSPACE_ROOT = ROOT.parent.parent  # workspace-stream-gen/

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ── .env 加载 ─────────────────────────────────────────────────────────────────
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


# ── DB 路径解析 ───────────────────────────────────────────────────────────────
def _resolve_db(db_arg: str) -> Path:
    if db_arg:
        return Path(db_arg)
    env = os.environ.get("FINANCE_DB_PATH", "").strip()
    if env:
        return Path(env)
    return WORKSPACE_ROOT / "user_data" / "finance_sources.db"


def _env_truthy(name: str, default: str = "1") -> bool:
    v = os.environ.get(name, default).strip().lower()
    return v in ("1", "true", "yes", "on")


def _append_social_intel_history_after_ingest_run(conn, run, storage_mod) -> dict:
    """``ingest.py run`` 收尾：用本时间窗内入库新闻跑 ``enhance_social_intelligence`` 并 append（``ingest_run``）。"""
    if not _env_truthy("FINANCE_SOCIAL_INTEL_HISTORY_ENABLED", "1"):
        return {"skipped": True, "reason": "FINANCE_SOCIAL_INTEL_HISTORY_ENABLED=0"}
    if not _env_truthy("FINANCE_SOCIAL_INTEL_INGEST_APPEND", "1"):
        return {"skipped": True, "reason": "FINANCE_SOCIAL_INTEL_INGEST_APPEND=0"}

    try:
        from fetchers.sentiment import extract_stock_mentions
        from fetchers.social_intelligence import enhance_social_intelligence

        try:
            lb_min = max(5, int(os.environ.get("FINANCE_SOCIAL_INTEL_INGEST_LOOKBACK_MINUTES", "120")))
        except ValueError:
            lb_min = 120
        try:
            max_news = max(50, int(os.environ.get("FINANCE_SOCIAL_INTEL_INGEST_MAX_NEWS", "500")))
        except ValueError:
            max_news = 500
        try:
            max_hist = max(1, int(os.environ.get("FINANCE_SOCIAL_INTEL_HISTORY_RUNS", "30")))
        except ValueError:
            max_hist = 30

        now = datetime.now(timezone.utc)
        alt_iso = (now - timedelta(minutes=lb_min)).isoformat(timespec="seconds")
        since_iso = min(run.started_at, alt_iso)

        rows = storage_mod.query_news_since_fetched_at(conn, since_iso=since_iso, limit=max_news)
        items: list[dict] = []
        seen: set[str] = set()
        for r in rows:
            dk = str(r.get("dedupe_key") or "")
            if dk and dk in seen:
                continue
            if dk:
                seen.add(dk)
            title = (r.get("clean_title") or r.get("raw_title") or "").strip()
            body = (r.get("clean_summary") or r.get("raw_content") or "").strip()
            txt = f"{title} {body}".strip()
            if not txt:
                continue
            items.append(
                {
                    "dedupe_key": dk,
                    "title": title or str(r.get("raw_title") or "")[:200],
                    "clean_text": body,
                    "source": str(r.get("source") or "unknown"),
                    "published_at": str(r.get("published_at") or ""),
                    "stock_mentions": extract_stock_mentions(txt),
                }
            )

        if len(items) < 2:
            return {"skipped": True, "reason": "too_few_news_rows", "news_in_window": len(items)}

        hist_rows = storage_mod.fetch_social_intel_run_history(conn, max_hist, source_kinds=None)
        hist_run_s = [float(x["headline_sentiment"]) for x in hist_rows]
        hist_run_b = [float(x["mean_buzz_score"]) for x in hist_rows]
        hist_fg = [float(x["fear_greed_index"]) for x in hist_rows]

        agg = enhance_social_intelligence(
            items,
            historical_fg=hist_fg if hist_fg else None,
            historical_run_sentiments=hist_run_s if hist_run_s else None,
            historical_run_buzz=hist_run_b if hist_run_b else None,
        )
        m = agg.get("aggregate_metrics") or {}
        if not m:
            return {"skipped": True, "reason": "empty_aggregate"}

        recorded = run.finished_at or now.isoformat(timespec="seconds")
        storage_mod.append_social_intel_run_history(
            conn,
            recorded_at=recorded,
            headline_sentiment=float(m.get("headline_sentiment") or 0.0),
            mean_buzz_score=float(m.get("mean_buzz_score") or 0.0),
            fear_greed_index=float(m.get("fear_greed_index") or 50.0),
            dedupe_unique_count=len(items),
            source_kind="ingest_run",
        )
        return {
            "ok": True,
            "dedupe_unique_count": len(items),
            "fear_greed_scope": m.get("fear_greed_scope"),
            "fear_greed_index": m.get("fear_greed_index"),
            "history_rows_loaded": len(hist_rows),
            "since_iso": since_iso,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("social_intel ingest_run append failed: %s", exc)
        return {"ok": False, "error": str(exc)[:300]}


# ── 采集器注册表 ──────────────────────────────────────────────────────────────
def _all_collectors():
    from collectors.sina_market import SinaMarketCollector
    from collectors.sina_live import SinaLiveCollector
    from collectors.cls_telegraph import ClsTelegraphCollector
    from collectors.rsshub import RSSHubCollector
    from collectors.deep_news import DeepNewsCollector
    from collectors.policy_gov import PolicyGovCollector
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


def _run_clean_loop(conn, storage_mod, cleaner, max_rounds: int) -> tuple[int, int]:
    """
    多轮清洗。max_rounds：0=不限；正数=最多执行这么多批。
    返回 (本轮回写条数, 执行的批次数)。
    """
    total_cleaned = 0
    rounds = 0
    while True:
        pending = storage_mod.get_pending_clean_items(conn, batch_size=cleaner.batch_size)
        if not pending:
            break
        cleaned, failed_keys = cleaner.clean_batch(pending)
        if cleaned:
            storage_mod.apply_cleaned_fields(conn, cleaned)
            total_cleaned += len(cleaned)
        if failed_keys:
            storage_mod.mark_clean_failed(conn, failed_keys, model=cleaner._model)
        rounds += 1
        if max_rounds > 0 and rounds >= max_rounds:
            break
    return total_cleaned, rounds


# ── cmd_run ───────────────────────────────────────────────────────────────────
def cmd_run(args: argparse.Namespace) -> None:
    from cleaner import LLMCleaner, max_clean_rounds_per_run
    from models.run import IngestRun
    import storage

    db_path = _resolve_db(args.db)
    conn = storage.init_db(db_path)

    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    keywords = [k for k in (args.keywords or "").split() if k.strip()]
    prune_days = int(args.prune_days)
    run = IngestRun(
        sources=",".join(sources),
        keywords=" ".join(keywords),
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    # ── 并发采集 ──────────────────────────────────────────────────────────────
    collectors = [c for c in _all_collectors() if c.is_enabled(sources)]
    all_news, all_market, all_sentiment, all_errors = [], [], [], []

    with ThreadPoolExecutor(max_workers=min(len(collectors), 6)) as pool:
        futures = {pool.submit(c._safe_fetch, keywords, args.max_items): c for c in collectors}
        for fut in as_completed(futures):
            result = fut.result()
            all_news.extend(result.news_items)
            all_market.extend(result.market_items)
            all_sentiment.extend(result.sentiment_items)
            all_errors.extend(result.errors)

    # ── 入库 ──────────────────────────────────────────────────────────────────
    inserted, updated = storage.upsert_news(conn, all_news)
    storage.upsert_market(conn, all_market)
    storage.upsert_sentiment(conn, all_sentiment)
    run.inserted = inserted
    run.updated = updated

    # ── LLM 清洗（多轮，直到无 pending 或达到 FINANCE_INGEST_LLM_CLEAN_MAX_ROUNDS_PER_RUN）──
    clean_rounds = 0
    if not args.no_clean:
        cleaner = LLMCleaner()
        if cleaner.is_available():
            max_r = max_clean_rounds_per_run()
            run.cleaned, clean_rounds = _run_clean_loop(conn, storage, cleaner, max_r)

    # ── prune ─────────────────────────────────────────────────────────────────
    run.pruned = storage.prune_old(conn, days=prune_days)

    # ── 收口 ──────────────────────────────────────────────────────────────────
    run.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    run.status = "ok"
    run.errors = all_errors
    storage.save_ingest_run(conn, run)

    si_hist = _append_social_intel_history_after_ingest_run(conn, run, storage)

    result = run.to_summary()
    result["db_path"] = str(db_path)
    result["clean_rounds"] = clean_rounds
    result["pending_clean_remain"] = storage.count_pending_clean(conn)
    result["social_intel_history_append"] = si_hist
    if all_errors:
        result["errors"] = all_errors[:10]

    # --preview：附加最近 5 条新闻标题（调试用）
    if args.preview:
        recent = storage.query_news(conn, since_hours=2, limit=5)
        result["preview_news"] = [
            {"title": r.get("clean_title") or r.get("raw_title"), "sector": r.get("sector")}
            for r in recent
        ]

    print(json.dumps(result, ensure_ascii=False, indent=2))


# ── cmd_init_db ───────────────────────────────────────────────────────────────
def cmd_init_db(args: argparse.Namespace) -> None:
    import storage
    db_path = _resolve_db(args.db)
    storage.init_db(db_path)
    print(json.dumps({"ok": True, "db_path": str(db_path)}, ensure_ascii=False))


# ── cmd_prune ─────────────────────────────────────────────────────────────────
def cmd_prune(args: argparse.Namespace) -> None:
    import storage
    db_path = _resolve_db(args.db)
    conn = storage.init_db(db_path)
    pruned = storage.prune_old(conn, days=int(args.days))
    print(json.dumps({"ok": True, "pruned": pruned, "days": args.days}, ensure_ascii=False))


def cmd_clean(args: argparse.Namespace) -> None:
    """仅执行 LLM 清洗（不抓取），用于积压补洗或与 cron 错开。"""
    from cleaner import LLMCleaner, max_clean_rounds_per_run

    import storage

    db_path = _resolve_db(args.db)
    conn = storage.init_db(db_path)
    reset_n = 0
    if getattr(args, "retry_failed", False):
        reset_n = storage.reset_failed_clean_to_pending(conn)

    cleaner = LLMCleaner()
    if not cleaner.is_available():
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "LLM 清洗未启用或缺少 BASE_URL/API_KEY；见 SKILL.md",
                    "pending_before": storage.count_pending_clean(conn),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        sys.exit(2)

    if args.max_rounds is not None:
        max_r = int(args.max_rounds)
    else:
        max_r = max_clean_rounds_per_run()

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
                "failed_reset_to_pending": reset_n,
                "db_path": str(db_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


# ── cmd_legacy（向后兼容，给 query_market_facts.py 调用）─────────────────────
def cmd_legacy(args: argparse.Namespace) -> None:
    """
    调用旧 pipeline.build_snapshot 路径，输出完整 JSON（含 markdown_summary）。
    保留此命令是为了 query_market_facts.py / preflight_topic.py 不断链。
    当所有上游调用方切换到 DB 模式后可废弃。
    """
    try:
        from pipeline import build_snapshot
    except ImportError:
        # pipeline.py 已迁走时的降级
        print(json.dumps({"ok": False, "error": "pipeline.py 已不可用，请使用 ingest.py run 入库后通过 DB 读取"}, ensure_ascii=False))
        return

    sources = [s.strip() for s in (args.sources or "market,news,social").split(",") if s.strip()]
    keywords = [k for k in (args.keywords or "").split() if k.strip()]
    snap = build_snapshot(sources, keywords, args.max_items)

    from _common import emit_json, write_json_atomic, write_text_atomic
    emit_json(snap)
    out_dir = (args.out_dir or "").strip()
    if out_dir:
        outp = Path(out_dir)
        write_json_atomic(outp / "snapshot.json", snap)
        write_text_atomic(outp / "snapshot.md", snap.get("markdown_summary", ""))


# ── cmd_repair_rsshub（原有回调接口，保留）───────────────────────────────────
_RSSHUB_UPDATE_SCRIPT = str(WORKSPACE_ROOT / "rsshub" / "update_rsshub.sh")


def cmd_repair_rsshub(args: argparse.Namespace) -> None:
    from scripts.ingest_legacy import cmd_repair_rsshub as _legacy_repair
    _legacy_repair(args)


# ── main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="finance-source-ingest (Finance Newsbox)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # run
    run_p = sub.add_parser("run", help="抓取并入库，stdout 极简 JSON")
    run_p.add_argument("--sources", default="market,news,social")
    run_p.add_argument("--keywords", default="")
    run_p.add_argument("--max-items", type=int, default=30)
    run_p.add_argument("--db", default="")
    run_p.add_argument("--prune-days", default=7, type=int)
    run_p.add_argument("--no-clean", action="store_true", help="跳过 LLM 清洗")
    run_p.add_argument("--preview", action="store_true", help="附加最近 5 条新闻供调试")
    run_p.set_defaults(func=cmd_run)

    # init-db
    init_p = sub.add_parser("init-db", help="仅初始化数据库建表")
    init_p.add_argument("--db", default="")
    init_p.set_defaults(func=cmd_init_db)

    # prune
    prune_p = sub.add_parser("prune", help="手动清理过期数据")
    prune_p.add_argument("--db", default="")
    prune_p.add_argument("--days", default=7, type=int)
    prune_p.set_defaults(func=cmd_prune)

    # clean（仅 LLM 清洗，不抓取）
    clean_p = sub.add_parser("clean", help="只对库内 pending 新闻做 LLM 清洗（不联网拉取）")
    clean_p.add_argument("--db", default="")
    clean_p.add_argument(
        "--max-rounds",
        type=int,
        default=None,
        help="最多洗多少批（每批 FINANCE_INGEST_LLM_CLEAN_BATCH_SIZE 条）；0=不限；默认读环境变量",
    )
    clean_p.add_argument(
        "--retry-failed",
        action="store_true",
        help="开始前将 llm_clean_status=failed 的条目改回 pending，便于重试（配合加大 MAX_TOKENS 等）",
    )
    clean_p.set_defaults(func=cmd_clean)

    # legacy（向后兼容旧 pipeline 输出）
    leg_p = sub.add_parser("legacy", help="旧 pipeline build_snapshot 兼容路径（输出完整 JSON+markdown）")
    leg_p.add_argument("--sources", default="market,news,social")
    leg_p.add_argument("--keywords", default="")
    leg_p.add_argument("--max-items", type=int, default=30)
    leg_p.add_argument("--out-dir", default="")
    leg_p.set_defaults(func=cmd_legacy)

    # repair-rsshub（原有回调接口）
    repair_p = sub.add_parser("repair-rsshub", help="RSSHub 修复回调（保留兼容）")
    repair_p.add_argument("--decision", default="ignore", choices=["confirm", "execute", "yes", "ignore", "no"])
    repair_p.add_argument("--sources", default="news")
    repair_p.add_argument("--keywords", default="")
    repair_p.add_argument("--max-items", type=int, default=30)
    repair_p.add_argument("--update-timeout", type=int, default=600)
    repair_p.add_argument("--token", default="")
    repair_p.set_defaults(func=cmd_repair_rsshub)

    args = parser.parse_args()
    fn = getattr(args, "func", None)
    if fn is None:
        parser.print_help()
        sys.exit(2)
    fn(args)


if __name__ == "__main__":
    main()
