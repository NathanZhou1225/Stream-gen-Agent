"""梯队二：MVP 财联社电报（AkShare）；原 RSS 主链路已停用。"""

from __future__ import annotations

import inspect
import json
import logging
from datetime import date, datetime, time
from typing import Any
from urllib import request as urlrequest

from _common import get_config_dir, now_iso

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
    return max(1, min(int(max_items), 10))


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


def _load_cls_dataframe(ak: Any, errors: list[dict[str, Any]]) -> tuple[Any, str, str]:
    df, tag = _try_stock_telegraph_cls_with_focus(ak)
    if df is not None and not getattr(df, "empty", True):
        return df, tag, "telegraph_cls"

    df_z = None
    try:
        df_z = _fetch_stock_info_global_cls(ak, "重点")
    except Exception as e:  # noqa: BLE001
        logger.warning("stock_info_global_cls(重点) 失败: %s", repr(e))

    if df_z is not None and not getattr(df_z, "empty", True):
        return df_z, "stock_info_global_cls(symbol=重点)", "重点"

    df_all = _fetch_stock_info_global_cls(ak, "全部")
    errors.append(
        {
            "source": "news",
            "stage": "akshare_cls_telegraph",
            "code": "CLS_TELEGRAPH_FOCUS_FALLBACK",
            "message": "财联社「重点」无数据或不可用，已使用「全部」",
            "hint": "stock_info_global_cls(symbol=全部)",
        }
    )
    return df_all, "stock_info_global_cls(symbol=全部)", "全部"


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


def _build_items_from_df(
    df: Any,
    col_title: str,
    col_body: str,
    pd: Any,
    keywords: list[str],
    cap: int,
    errors: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    pool_n = min(len(df), max(cap * 8, 24, cap + 5))
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
            out = filtered[:cap]
        else:
            out = raw_items[:3]
            keyword_fallback = True
            errors.append(
                {
                    "source": "news",
                    "stage": "keywords",
                    "code": "NEWS_KEYWORD_FALLBACK",
                    "message": "关键词 OR 过滤无命中，已退回最新 3 条原文",
                    "hint": str(kws[:12]),
                }
            )
    else:
        out = raw_items[:cap]

    return out[:cap], keyword_fallback


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
            }, errors

        items, keyword_fallback = _build_items_from_df(
            df, col_title, col_body, pd, keywords, cap, errors
        )

        return {
            "as_of": now_iso(),
            "items": items,
            "sources_used": ["财联社电报"],
            "source_primary": f"akshare:{api_tag}",
            "cls_symbol": cls_symbol,
            "keyword_fallback": keyword_fallback,
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
        }, errors
