"""梯队二：MVP 财联社电报（AkShare）；原 RSS 主链路已停用。"""

from __future__ import annotations

import inspect
import json
import logging
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib import request as urlrequest

from _common import get_config_dir, now_iso

from .sector_keywords import SECTOR_KEYWORDS, SECTOR_ORDER, group_by_sector, tag_news_items

logger = logging.getLogger(__name__)

RSS_LEGACY_SNIPPET = r"""
# RSS 主路径已停用；恢复见 git / MEMORY。
"""


def _load_sources() -> list[dict[str, Any]]:
    path = get_config_dir() / "news_sources.json"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    srcs = data.get("sources") or []
    if not isinstance(srcs, list):
        return []
    return [x for x in srcs if isinstance(x, dict)]


def _fetch_url(url: str, timeout: int = 12) -> bytes:
    req = urlrequest.Request(url, headers={"User-Agent": "Mozilla/5.0 finance-source-ingest/0.1"})
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _news_cap(max_items: int) -> int:
    """单轮输出条数上限（六大板块优先）；默认最多 48 条。"""
    return max(1, min(int(max_items), 48))


def _try_stock_telegraph_cls_with_focus(ak: Any) -> tuple[Any | None, str]:
    fn = getattr(ak, "stock_telegraph_cls", None)
    if not callable(fn):
        return None, ""
    try:
        sig = inspect.signature(fn)
        param_names = list(sig.parameters.keys())
    except (TypeError, ValueError):
        param_names = []

    if not param_names:
        try:
            return fn(), "stock_telegraph_cls()"
        except Exception:  # noqa: BLE001
            return None, ""

    if "symbol" in sig.parameters:
        try:
            return fn(symbol="重点"), "stock_telegraph_cls(symbol=重点)"
        except Exception:  # noqa: BLE001
            try:
                return fn(symbol="全部"), "stock_telegraph_cls(symbol=全部)"
            except Exception:  # noqa: BLE001
                pass
    try:
        return fn(), "stock_telegraph_cls()"
    except Exception:  # noqa: BLE001
        return None, ""


def _fetch_stock_info_global_cls(ak: Any, symbol: str) -> Any:
    return ak.stock_info_global_cls(symbol=symbol)


def _fetch_cls_nodeapi_dataframe(pd: Any, *, rn: int = 100) -> Any:
    url = f"https://www.cls.cn/nodeapi/telegraphList?app=CailianpressWeb&os=web&refresh_type=1&rn={rn}&sv=8.4.6"
    raw = _fetch_url(url, timeout=12)
    data = json.loads(raw.decode("utf-8", errors="replace"))
    rows = (((data or {}).get("data") or {}).get("roll_data")) or []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        content = str(row.get("content") or row.get("brief") or title).strip()
        ctime = row.get("ctime")
        try:
            dt = datetime.utcfromtimestamp(int(ctime)) + timedelta(hours=8)
        except Exception:  # noqa: BLE001
            dt = datetime.now()
        if title or content:
            out.append(
                {
                    "标题": title or content,
                    "内容": content or title,
                    "发布日期": dt.date(),
                    "发布时间": dt.time().replace(microsecond=0),
                }
            )
    df = pd.DataFrame(out)
    if not df.empty:
        df.sort_values(["发布日期", "发布时间"], inplace=True)
        df.reset_index(inplace=True, drop=True)
    return df


def _load_cls_dataframe(ak: Any, errors: list[dict[str, Any]]) -> tuple[Any, str, str]:
    frames: list[Any] = []
    tags: list[str] = []
    symbols: list[str] = []

    df, tag = _try_stock_telegraph_cls_with_focus(ak)
    if df is not None and not getattr(df, "empty", True):
        frames.append(df)
        tags.append(tag or "stock_telegraph_cls")
        symbols.append("telegraph_cls")

    df_z = None
    try:
        df_z = _fetch_stock_info_global_cls(ak, "重点")
    except Exception as e:  # noqa: BLE001
        logger.warning("stock_info_global_cls(重点) 失败: %s", repr(e))

    if df_z is not None and not getattr(df_z, "empty", True):
        frames.append(df_z)
        tags.append("stock_info_global_cls(symbol=重点)")
        symbols.append("重点")

    df_all = None
    try:
        df_all = _fetch_stock_info_global_cls(ak, "全部")
    except Exception as e:  # noqa: BLE001
        logger.warning("stock_info_global_cls(全部) 失败: %s", repr(e))
        if not frames:
            raise
        errors.append(
            {
                "source": "news",
                "stage": "akshare_cls_telegraph",
                "code": "CLS_TELEGRAPH_ALL_FAILED",
                "message": repr(e),
                "hint": "已使用财联社重点池；六大板块补位可能不足",
            }
        )

    if df_all is not None and not getattr(df_all, "empty", True):
        frames.append(df_all)
        tags.append("stock_info_global_cls(symbol=全部)")
        symbols.append("全部")

    import pandas as pd  # noqa: PLC0415

    try:
        df_node = _fetch_cls_nodeapi_dataframe(pd, rn=100)
        if df_node is not None and not getattr(df_node, "empty", True):
            frames.append(df_node)
            tags.append("cls_nodeapi(rn=100)")
            symbols.append("nodeapi")
    except Exception as e:  # noqa: BLE001
        logger.warning("cls nodeapi 宽池失败: %s", repr(e))
        if not frames:
            errors.append(
                {
                    "source": "news",
                    "stage": "cls_nodeapi",
                    "code": "CLS_NODEAPI_FAILED",
                    "message": repr(e),
                    "hint": "https://www.cls.cn/nodeapi/telegraphList",
                }
            )

    if not frames:
        return _fetch_stock_info_global_cls(ak, "全部"), "stock_info_global_cls(symbol=全部)", "全部"

    if len(frames) == 1:
        return frames[0], tags[0], symbols[0]

    merged = pd.concat(frames, ignore_index=True)
    dedup_cols = [c for c in ("标题", "发布日期", "发布时间") if c in merged.columns]
    if dedup_cols:
        merged = merged.drop_duplicates(subset=dedup_cols, keep="last")
    return merged, "+".join(tags), "+".join(symbols)


def _format_published_at(row: Any, pd: Any) -> str:
    try:
        if "发布日期" in row.index and "发布时间" in row.index:  # type: ignore[attr-defined]
            d_raw = row.get("发布日期")
            t_raw = row.get("发布时间")
            if pd.notna(d_raw) and pd.notna(t_raw):
                if isinstance(d_raw, datetime):
                    d_part = d_raw.date()
                elif isinstance(d_raw, date):
                    d_part = d_raw
                else:
                    d_part = pd.to_datetime(d_raw, errors="coerce")
                    if pd.isna(d_part):
                        return ""
                    d_part = d_part.date() if hasattr(d_part, "date") else d_part
                if isinstance(t_raw, datetime):
                    t_part = t_raw.time()
                elif isinstance(t_raw, time):
                    t_part = t_raw
                else:
                    tt = pd.to_datetime(str(t_raw), errors="coerce")
                    if pd.isna(tt):
                        return ""
                    t_part = tt.time() if hasattr(tt, "time") else time(0, 0, 0)
                dt = datetime.combine(d_part, t_part)
                return dt.strftime("%Y-%m-%d %H:%M:%S")
        if "发布时间" in row.index:
            ts = row.get("发布时间")
            if pd.notna(ts):
                if isinstance(ts, datetime):
                    return ts.strftime("%Y-%m-%d %H:%M:%S")
                parsed = pd.to_datetime(ts, errors="coerce")
                if pd.notna(parsed):
                    return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _hhmm_for_markdown(published_at: str) -> str:
    if not published_at or len(published_at) < 16:
        return "--:--"
    try:
        return published_at[11:16]
    except Exception:  # noqa: BLE001
        return "--:--"


def news_hhmm_for_markdown(published_at: str) -> str:
    return _hhmm_for_markdown(published_at)


def _row_to_news_item(row: Any, col_title: str, col_body: str, pd: Any) -> dict[str, Any] | None:
    raw_title = row.get(col_title, "")
    title = "" if raw_title is None or (isinstance(raw_title, float) and pd.isna(raw_title)) else str(raw_title).strip()
    raw_body = row.get(col_body)
    if raw_body is None or (isinstance(raw_body, float) and pd.isna(raw_body)):
        body = ""
    else:
        body = str(raw_body).strip()
    clean_text = body if body else title
    if not title and not clean_text:
        return None
    published_at = _format_published_at(row, pd)
    return {"title": title or clean_text, "clean_text": clean_text, "published_at": published_at}


def _keyword_or_match(item: dict[str, Any], keywords: list[str]) -> bool:
    blob = f"{item.get('title') or ''} {item.get('clean_text') or ''}".lower()
    return any(k.strip().lower() in blob for k in keywords if k.strip())


def _parse_published_ts(val: Any) -> datetime | None:
    if val is None:
        return None
    s = str(val).strip()
    if len(s) < 10:
        return None
    s19 = s[:19] if len(s) >= 19 else s
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s19, fmt)
        except ValueError:
            continue
    return None


def _enrich_sector_buckets(
    all_tagged: list[dict[str, Any]],
    *,
    max_per_sector: int,
    min_fill: int,
) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    """
    六大板块各凑满展示位：优先正式标签命中；不足时用宽池内「板块关键词」回溯最近快讯（非最新也可）。
    返回 (buckets, relax_used)。
    """
    buckets: dict[str, list[dict[str, Any]]] = {s: [] for s in SECTOR_ORDER}
    def item_key(it: dict[str, Any]) -> tuple[str, str]:
        return (str(it.get("title") or ""), str(it.get("published_at") or ""))

    sorted_items = sorted(
        [x for x in all_tagged if isinstance(x, dict)],
        key=lambda x: _parse_published_ts(x.get("published_at")) or datetime.min,
        reverse=True,
    )

    def dedup_append(sec: str, it: dict[str, Any], source: str) -> None:
        cur = buckets[sec]
        if len(cur) >= max_per_sector:
            return
        k = item_key(it)
        if any(item_key(x) == k for x in cur):
            return
        neo = dict(it)
        neo["sector_line_source"] = source
        cur.append(neo)

    for it in sorted_items:
        for sec in it.get("sector_tags") or []:
            if sec in buckets:
                dedup_append(sec, it, "tagged")

    for sec in SECTOR_ORDER:
        if len(buckets[sec]) >= min_fill:
            continue
        kws = SECTOR_KEYWORDS.get(sec, ())
        for it in sorted_items:
            if len(buckets[sec]) >= max_per_sector:
                break
            blob = f"{it.get('title') or ''}{it.get('clean_text') or ''}"
            if not any(k and (k in blob) for k in kws):
                continue
            if any(item_key(it) == item_key(x) for x in buckets[sec]):
                continue
            tags = it.get("sector_tags") or []
            src = "recent_keyword" if sec not in tags else "tagged_catchup"
            dedup_append(sec, it, src)

    relax_used = any(
        x.get("sector_line_source") in ("recent_keyword", "tagged_catchup")
        for lst in buckets.values()
        for x in lst
    )
    return buckets, relax_used


def _build_items_from_df(
    df: Any,
    col_title: str,
    col_body: str,
    pd: Any,
    keywords: list[str],
    cap: int,
    errors: list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    bool,
    dict[str, list[dict[str, Any]]],
    list[dict[str, Any]],
    bool,
    list[dict[str, Any]],
    bool,
]:
    """宽池打标签 → 主列表 + 六大板块分桶（含关键词回溯填桶）。"""
    pool_n = min(len(df), max(520, cap * 22, cap + 48))
    tail = df.tail(pool_n) if pool_n else df.iloc[0:0]
    newest_first = list(tail.iloc[::-1].iterrows())
    raw_items: list[dict[str, Any]] = []
    for _, row in newest_first:
        it = _row_to_news_item(row, col_title, col_body, pd)
        if it:
            raw_items.append(it)

    keyword_fallback = False
    kws = [k for k in (keywords or []) if str(k).strip()]
    if kws:
        filtered = [x for x in raw_items if _keyword_or_match(x, kws)]
        if filtered:
            working = filtered
        else:
            working = raw_items[: max(cap, 12)]
            keyword_fallback = True
            errors.append(
                {
                    "source": "news",
                    "stage": "keywords",
                    "code": "NEWS_KEYWORD_FALLBACK",
                    "message": "关键词 OR 过滤无命中，已退回宽池原文再打板块标签",
                    "hint": str(kws[:12]),
                }
            )
    else:
        working = raw_items

    full_tagged = tag_news_items(raw_items)
    tagged = tag_news_items(working)
    sector_hits = [x for x in tagged if x.get("sector_tags")]
    sector_filter_fallback = False

    if sector_hits:
        out = sector_hits[:cap]
    else:
        out = tagged[: min(5, cap)]
        sector_filter_fallback = True
        errors.append(
            {
                "source": "news",
                "stage": "sector_filter",
                "code": "SECTOR_FILTER_EMPTY",
                "message": "本轮快讯未命中六大板块关键词，已降级展示最新若干条原文",
                "hint": "科技/新能源/港股/黄金/有色/银行",
            }
        )

    items_by_sector, sector_relax_used = _enrich_sector_buckets(
        full_tagged,
        max_per_sector=max(4, min(8, cap // 2 or 4)),
        min_fill=1,
    )
    _, unclassified = group_by_sector(out)
    other_flash = [x for x in full_tagged if isinstance(x, dict) and not (x.get("sector_tags") or [])][:28]

    return (
        out[:cap],
        keyword_fallback,
        items_by_sector,
        unclassified,
        sector_filter_fallback,
        other_flash,
        sector_relax_used,
    )


def fetch_news_section(
    keywords: list[str],
    max_items: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    cap = _news_cap(max_items)

    try:
        import akshare as ak  # noqa: PLC0415
        import pandas as pd  # noqa: PLC0415
    except ImportError as e:
        errors.append(
            {
                "source": "news",
                "stage": "import",
                "code": "AKSHARE_IMPORT_ERROR",
                "message": str(e),
                "hint": "pip install -r requirements.txt（含 akshare、pandas）",
            }
        )
        return {
            "as_of": now_iso(),
            "items": [],
            "sources_used": [],
            "source_primary": "akshare:cls_telegraph",
            "keyword_fallback": False,
            "items_by_sector": {},
            "sector_filter_fallback": False,
            "items_unclassified": [],
            "items_other_flash": [],
            "sector_relax_backfill": False,
        }, errors

    try:
        df, api_tag, cls_symbol = _load_cls_dataframe(ak, errors)
    except Exception as e:  # noqa: BLE001
        logger.warning("财联社电报拉取失败: %s", repr(e))
        errors.append(
            {
                "source": "news",
                "stage": "akshare_cls_telegraph",
                "code": "CLS_TELEGRAPH_FAILED",
                "message": repr(e),
                "hint": "网络波动或财联社/东财接口变更；稍后重试或升级 akshare",
            }
        )
        return {
            "as_of": now_iso(),
            "items": [],
            "sources_used": [],
            "source_primary": "akshare:cls_telegraph",
            "keyword_fallback": False,
            "items_by_sector": {},
            "sector_filter_fallback": False,
            "items_unclassified": [],
            "items_other_flash": [],
            "sector_relax_backfill": False,
        }, errors

    try:
        if df is None or getattr(df, "empty", True):
            errors.append(
                {
                    "source": "news",
                    "stage": "akshare_cls_telegraph",
                    "code": "CLS_TELEGRAPH_EMPTY",
                    "message": "财联社电报返回空表",
                    "hint": "接口正常但无数据；稍后重试",
                }
            )
            return {
                "as_of": now_iso(),
                "items": [],
                "sources_used": ["财联社电报"],
                "source_primary": f"akshare:{api_tag}",
                "cls_symbol": cls_symbol,
                "keyword_fallback": False,
                "items_by_sector": {},
                "sector_filter_fallback": False,
                "items_unclassified": [],
                "items_other_flash": [],
                "sector_relax_backfill": False,
            }, errors

        col_title = "标题" if "标题" in df.columns else None
        col_body = "内容" if "内容" in df.columns else None
        if not col_title or not col_body:
            errors.append(
                {
                    "source": "news",
                    "stage": "akshare_cls_telegraph",
                    "code": "CLS_TELEGRAPH_SCHEMA",
                    "message": f"DataFrame 缺少预期列，当前列: {list(df.columns)}",
                    "hint": "升级 akshare 或检查 stock_info_global_cls 字段变更",
                }
            )
            return {
                "as_of": now_iso(),
                "items": [],
                "sources_used": ["财联社电报"],
                "source_primary": f"akshare:{api_tag}",
                "cls_symbol": cls_symbol,
                "keyword_fallback": False,
                "items_by_sector": {},
                "sector_filter_fallback": False,
                "items_unclassified": [],
                "items_other_flash": [],
                "sector_relax_backfill": False,
            }, errors

        (
            items,
            keyword_fallback,
            items_by_sector,
            items_unclassified,
            sector_filter_fallback,
            items_other_flash,
            sector_relax_used,
        ) = _build_items_from_df(df, col_title, col_body, pd, keywords, cap, errors)

        return {
            "as_of": now_iso(),
            "items": items,
            "sources_used": ["财联社电报"],
            "source_primary": f"akshare:{api_tag}",
            "cls_symbol": cls_symbol,
            "keyword_fallback": keyword_fallback,
            "items_by_sector": items_by_sector,
            "sector_filter_fallback": sector_filter_fallback,
            "items_unclassified": items_unclassified,
            "items_other_flash": items_other_flash,
            "sector_relax_backfill": sector_relax_used,
        }, errors
    except Exception as e:  # noqa: BLE001
        logger.warning("财联社电报解析失败: %s", repr(e))
        errors.append(
            {
                "source": "news",
                "stage": "akshare_cls_telegraph",
                "code": "CLS_TELEGRAPH_PARSE_FAILED",
                "message": repr(e),
                "hint": "DataFrame 解析异常；检查 pandas/列类型",
            }
        )
        return {
            "as_of": now_iso(),
            "items": [],
            "sources_used": ["财联社电报"],
            "source_primary": "akshare:cls_telegraph",
            "keyword_fallback": False,
            "items_by_sector": {},
            "sector_filter_fallback": False,
            "items_unclassified": [],
            "items_other_flash": [],
            "sector_relax_backfill": False,
        }, errors
