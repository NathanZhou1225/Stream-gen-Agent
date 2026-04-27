"""梯队一：AkShare 主路径 + 可选新浪 hq 海外备源（urllib）。"""

from __future__ import annotations

import logging
import math
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from typing import Any, Callable
from urllib import request as _urlreq
from urllib.error import URLError

from _common import now_iso

from fetchers.models import normalize_index_item

logger = logging.getLogger(__name__)


def _akshare_retry_count() -> int:
    """环境变量 FINANCE_SOURCE_AKSHARE_RETRIES，默认 3，范围 1–8。"""
    raw = os.environ.get("FINANCE_SOURCE_AKSHARE_RETRIES", "3").strip() or "3"
    try:
        n = int(raw)
    except ValueError:
        return 3
    return max(1, min(n, 8))


def _akshare_primary_timeout_sec() -> float:
    """A 股大盘主链路 AkShare 单次调用的总超时（秒），默认 5，范围 1–60。"""
    raw = os.environ.get("FINANCE_SOURCE_AKSHARE_PRIMARY_TIMEOUT", "5").strip() or "5"
    try:
        t = float(raw)
    except ValueError:
        return 5.0
    return max(1.0, min(t, 60.0))


def _call_with_retries(fn: Callable[[], Any], *, attempts: int) -> Any:
    """AkShare 经 requests 访问东财，易遇 RemoteDisconnected；短暂重试可提高成功率。"""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            if i >= attempts - 1:
                raise last
            sleep_s = min(5.0, 0.7 * (2**i) + random.random() * 0.5)
            time.sleep(sleep_s)
    raise last  # pragma: no cover


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        if hasattr(v, "item"):
            v = v.item()
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return None
        return round(x, 4)
    except (TypeError, ValueError):
        return None


# ---------- 海外：新浪财经（与 streamy-content-gen fetch_market 对齐，代码独立） ----------

OVERSEAS_INDICES = [
    {"sina_code": "gb_$dji", "name": "道琼斯"},
    {"sina_code": "gb_$inx", "name": "标普500"},
    {"sina_code": "gb_$ixic", "name": "纳斯达克"},
]
VIX_CODE = "gb_vxx"
COMMODITIES = [
    {"sina_code": "hf_GC", "name": "COMEX 黄金"},
    {"sina_code": "hf_CL", "name": "WTI 原油"},
]

# A 股三大指数：新浪 list 与输出 code（与 AkShare 东财代码习惯对齐）
A_SHARE_INDEX_NAMES = ["上证指数", "深证成指", "创业板指"]
_NAME_ORDER = {n: i for i, n in enumerate(A_SHARE_INDEX_NAMES)}
A_SHARE_SINA_FALLBACK_SPECS: list[dict[str, str]] = [
    {"sina_code": "s_sh000001", "name": "上证指数", "code": "000001"},
    {"sina_code": "s_sz399001", "name": "深证成指", "code": "399001"},
    {"sina_code": "s_sz399006", "name": "创业板指", "code": "399006"},
]


def _sina_fetch(codes: list[str], *, timeout: float = 8.0) -> dict[str, list[str]]:
    url = "http://hq.sinajs.cn/list=" + ",".join(codes)
    req = _urlreq.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) finance-source-ingest/0.1",
            "Referer": "https://finance.sina.com.cn/",
        },
    )
    with _urlreq.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("gbk", errors="replace")
    out: dict[str, list[str]] = {}
    for line in raw.strip().splitlines():
        if "=" not in line:
            continue
        left, right = line.split("=", 1)
        code = left.replace("var hq_str_", "").strip()
        payload = right.strip().strip('";').strip('"')
        out[code] = [x.strip() for x in payload.split(",")]
    return out


def _akshare_stock_zh_index_spot_em_with_timeout(ak: Any, timeout_sec: float) -> Any:
    """AkShare 无原生超时参数，用线程池限定单次东财拉取_wall_时间。"""

    def _run() -> Any:
        return ak.stock_zh_index_spot_em()

    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(_run)
        return fut.result(timeout=timeout_sec)


def _indices_from_akshare_df(df: Any) -> list[dict[str, Any]]:
    if df is None or getattr(df, "empty", True):
        return []
    sub = df[df["名称"].isin(A_SHARE_INDEX_NAMES)]
    if sub.empty:
        return []
    items: list[dict[str, Any]] = []
    for _, row in sub.iterrows():
        items.append(
            normalize_index_item(
                code=str(row.get("代码", "") or ""),
                name=str(row.get("名称", "") or ""),
                close=_to_float(row.get("最新价")),
                pct_change=_to_float(row.get("涨跌幅")),
            )
        )
    items.sort(key=lambda x: _NAME_ORDER.get(x["name"], 99))
    return items


def _primary_indices_complete(items: list[dict[str, Any]] | None) -> bool:
    if not items or len(items) != 3:
        return False
    if {x["name"] for x in items} != set(A_SHARE_INDEX_NAMES):
        return False
    return all(x.get("close") is not None for x in items)


def _fetch_a_share_indices_from_sina_trinity(*, timeout: float = 8.0) -> list[dict[str, Any]]:
    codes = [s["sina_code"] for s in A_SHARE_SINA_FALLBACK_SPECS]
    raw = _sina_fetch(codes, timeout=timeout)
    items: list[dict[str, Any]] = []
    for spec in A_SHARE_SINA_FALLBACK_SPECS:
        payload = raw.get(spec["sina_code"], [])
        if len(payload) < 4:
            items.append(
                normalize_index_item(
                    code=spec["code"],
                    name=spec["name"],
                    close=None,
                    pct_change=None,
                )
            )
            continue
        items.append(
            normalize_index_item(
                code=spec["code"],
                name=str(payload[0] or spec["name"]),
                close=_to_float(payload[1]),
                pct_change=_to_float(payload[3]),
            )
        )
    return items


def _fallback_indices_usable(items: list[dict[str, Any]]) -> bool:
    if len(items) != 3:
        return False
    return any(x.get("close") is not None for x in items)


def _placeholder_trinity_items() -> list[dict[str, Any]]:
    return [
        normalize_index_item(
            code=s["code"],
            name=s["name"],
            close=None,
            pct_change=None,
        )
        for s in A_SHARE_SINA_FALLBACK_SPECS
    ]


def _collect_a_share_indices(ak: Any, out: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    timeout_sec = _akshare_primary_timeout_sec()
    primary_items: list[dict[str, Any]] | None = None
    primary_fail: str | None = None

    try:
        df = _akshare_stock_zh_index_spot_em_with_timeout(ak, timeout_sec)
        primary_items = _indices_from_akshare_df(df)
    except FuturesTimeout as e:
        primary_fail = f"AkShare 超时 ({timeout_sec}s): {e!r}"
        logger.warning("%s — 将降级新浪三大指数", primary_fail)
    except Exception as e:  # noqa: BLE001
        primary_fail = f"AkShare 异常: {e!r}"
        logger.warning("%s — 将降级新浪三大指数", primary_fail)

    if primary_fail is None and not _primary_indices_complete(primary_items):
        primary_fail = "主链路返回空或不完整（不足三条或缺少收盘价）"
        logger.warning("%s — 将降级新浪三大指数", primary_fail)

    if _primary_indices_complete(primary_items):
        out["a_share_indices"] = {
            "source": "akshare:stock_zh_index_spot_em",
            "items": primary_items,
        }
        return

    fb: list[dict[str, Any]] = []
    try:
        fb = _fetch_a_share_indices_from_sina_trinity(timeout=8.0)
    except Exception as e:  # noqa: BLE001
        logger.warning("新浪三大指数降级失败: %s", repr(e))
        errors.append(
            {
                "source": "market",
                "stage": "a_share_indices_sina_fallback",
                "code": "SINA_TRINITY_FAILED",
                "message": repr(e),
                "hint": "urllib 请求新浪 list 接口失败；检查网络或接口是否变更",
            }
        )
        fb = []

    if _fallback_indices_usable(fb):
        out["a_share_indices"] = {
            "source": "sina:hq.sinajs.cn,list=s_sh000001,s_sz399001,s_sz399006",
            "items": fb,
        }
        errors.append(
            {
                "source": "market",
                "stage": "a_share_indices",
                "code": "A_SHARE_INDICES_FALLBACK_SINA",
                "message": primary_fail or "unknown_primary_failure",
                "hint": "已使用新浪财经原生接口填充三大指数，items 字段与 AkShare 主链路一致",
            }
        )
        return

    if primary_fail:
        msg_tail = f"；新浪降级无有效收盘价: {fb!r}" if fb else "；新浪降级无数据"
    else:
        msg_tail = "新浪降级无有效数据"
    errors.append(
        {
            "source": "market",
            "stage": "a_share_indices",
            "code": "A_SHARE_INDICES_UNAVAILABLE",
            "message": (primary_fail or "主备均失败") + msg_tail,
            "hint": "东财与新浪均不可用；已输出三条占位结构便于下游解析",
        }
    )
    merged = fb if fb else (primary_items or [])
    if len(merged) != 3:
        merged = _placeholder_trinity_items()
    out["a_share_indices"] = {
        "source": "akshare+sina:degraded_empty",
        "items": merged,
    }


def fetch_overseas_indices_and_vix() -> dict[str, Any]:
    codes = [cfg["sina_code"] for cfg in OVERSEAS_INDICES] + [VIX_CODE]
    raw = _sina_fetch(codes)
    us_indices = []
    for cfg in OVERSEAS_INDICES:
        data = raw.get(cfg["sina_code"], [])
        if len(data) < 6:
            us_indices.append(
                {
                    "code": cfg["sina_code"],
                    "name": cfg["name"],
                    "close": None,
                    "pct_change": None,
                    "note": "新浪接口返回不完整",
                }
            )
            continue
        us_indices.append(
            {
                "code": cfg["sina_code"],
                "name": cfg["name"],
                "close": _to_float(data[1]),
                "pct_change": _to_float(data[2]),
                "change_abs": _to_float(data[4]),
                "prev_close": _to_float(data[5]),
            }
        )
    vix_data = raw.get(VIX_CODE, [])
    if len(vix_data) >= 6:
        vix = {
            "value": _to_float(vix_data[1]),
            "pct_change": _to_float(vix_data[2]),
            "source": "sina:gb_vxx",
            "note": "VXX ETF 作为 VIX 代理，方向性一致但绝对值不同",
        }
    else:
        vix = {"value": None, "pct_change": None, "source": "sina:gb_vxx", "note": "VXX 数据获取失败"}
    return {"source": "sina:hq.sinajs.cn", "us_indices": us_indices, "vix": vix}


def fetch_commodities() -> dict[str, Any]:
    codes = [c["sina_code"] for c in COMMODITIES]
    raw = _sina_fetch(codes)
    items = []
    for cfg in COMMODITIES:
        data = raw.get(cfg["sina_code"], [])
        if len(data) < 8:
            items.append(
                {
                    "code": cfg["sina_code"],
                    "name": cfg["name"],
                    "close": None,
                    "pct_change": None,
                    "note": "新浪接口返回不完整",
                }
            )
            continue
        now = _to_float(data[0])
        prev = None
        for field in data[5:9]:
            if field and _to_float(field) is not None:
                prev = _to_float(field)
                break
        pct = None
        if now is not None and prev is not None and prev != 0:
            pct = round((now - prev) / prev * 100, 2)
        items.append({"code": cfg["sina_code"], "name": cfg["name"], "close": now, "pct_change": pct})
    return {"source": "sina:hq.sinajs.cn", "items": items}


def fetch_market_section(overseas_stub_enabled: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    out: dict[str, Any] = {
        "source_primary": "akshare",
        "as_of": now_iso(),
        "a_share_indices": None,
        "northbound": None,
        "industry_rank": None,
        "overseas_stub": None,
    }

    try:
        import akshare as ak  # noqa: PLC0415
        import pandas as pd  # noqa: PLC0415
    except ImportError as e:
        errors.append(
            {
                "source": "market",
                "stage": "import",
                "code": "AKSHARE_IMPORT_ERROR",
                "message": str(e),
                "hint": "请在本 skill 目录执行: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt",
            }
        )
        try:
            fb = _fetch_a_share_indices_from_sina_trinity(timeout=8.0)
            if _fallback_indices_usable(fb):
                out["a_share_indices"] = {
                    "source": "sina:hq.sinajs.cn,list=s_sh000001,s_sz399001,s_sz399006",
                    "items": fb,
                }
                errors.append(
                    {
                        "source": "market",
                        "stage": "a_share_indices",
                        "code": "A_SHARE_INDICES_FALLBACK_SINA",
                        "message": "AkShare 未安装，已仅用新浪三大指数",
                        "hint": "items 结构与主链路一致；安装 AkShare 后可走东财主链路",
                    }
                )
            else:
                out["a_share_indices"] = {
                    "source": "sina:placeholder",
                    "items": _placeholder_trinity_items(),
                }
        except Exception as ex:  # noqa: BLE001
            logger.warning("AkShare 缺失且新浪三大指数降级失败: %s", repr(ex))
            out["a_share_indices"] = {
                "source": "sina:placeholder",
                "items": _placeholder_trinity_items(),
            }
            errors.append(
                {
                    "source": "market",
                    "stage": "a_share_indices_sina_fallback",
                    "code": "SINA_TRINITY_FAILED",
                    "message": repr(ex),
                    "hint": "无 AkShare 且新浪接口不可用",
                }
            )
        if overseas_stub_enabled:
            try:
                out["overseas_stub"] = {
                    "overseas": fetch_overseas_indices_and_vix(),
                    "commodities": fetch_commodities(),
                }
            except (URLError, OSError, ValueError, KeyError, IndexError) as ex:
                errors.append(
                    {
                        "source": "market",
                        "stage": "overseas_stub",
                        "code": "SINA_HQ_FAILED",
                        "message": str(ex),
                        "hint": "检查网络或新浪接口是否变更",
                    }
                )
        return out, errors

    retries = _akshare_retry_count()

    # --- A 股主要指数（AkShare 短超时主链路 + 新浪三大指数 urllib 降级）---
    _collect_a_share_indices(ak, out, errors)

    # --- 北向资金（当日汇总表）---
    try:
        df = _call_with_retries(lambda: ak.stock_hsgt_fund_flow_summary_em(), attempts=retries)
        nb = df[df["资金方向"] == "北向"]
        net_series = pd.to_numeric(nb["成交净买额"], errors="coerce")
        net = float(net_series.sum()) if len(net_series) else None
        rows = []
        for _, row in nb.iterrows():
            rows.append(
                {
                    "board": str(row.get("板块", "") or ""),
                    "net_buy_yi": _to_float(row.get("成交净买额")),
                    "flow_net_yi": _to_float(row.get("资金净流入")),
                }
            )
        out["northbound"] = {
            "source": "akshare:stock_hsgt_fund_flow_summary_em",
            "aggregate_net_buy_yi": net,
            "rows": rows,
        }
    except Exception as e:  # noqa: BLE001
        errors.append(
            {
                "source": "market",
                "stage": "akshare_northbound",
                "code": "AKSHARE_CALL_FAILED",
                "message": repr(e),
                "hint": "可增大 FINANCE_SOURCE_AKSHARE_RETRIES 或稍后重试",
            }
        )

    # --- 行业涨跌幅 Top5（按今日涨跌幅降序）---
    try:
        df = _call_with_retries(
            lambda: ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流"),
            attempts=retries,
        )
        work = df.copy()
        work["_pct"] = pd.to_numeric(work["今日涨跌幅"], errors="coerce")
        work = work.sort_values("_pct", ascending=False).head(5)
        items = []
        for _, row in work.iterrows():
            items.append(
                {
                    "name": str(row.get("名称", "") or ""),
                    "pct_change": _to_float(row.get("今日涨跌幅")),
                    "main_net_inflow": _to_float(row.get("今日主力净流入-净额")),
                }
            )
        out["industry_rank"] = {
            "source": "akshare:stock_sector_fund_flow_rank",
            "sort": "今日涨跌幅_desc",
            "items": items,
        }
    except Exception as e:  # noqa: BLE001
        errors.append(
            {
                "source": "market",
                "stage": "akshare_industry",
                "code": "AKSHARE_CALL_FAILED",
                "message": repr(e),
                "hint": "可增大 FINANCE_SOURCE_AKSHARE_RETRIES 或稍后重试",
            }
        )

    if overseas_stub_enabled:
        try:
            out["overseas_stub"] = {
                "overseas": fetch_overseas_indices_and_vix(),
                "commodities": fetch_commodities(),
            }
        except (URLError, OSError, ValueError, KeyError, IndexError) as ex:
            errors.append(
                {
                    "source": "market",
                    "stage": "overseas_stub",
                    "code": "SINA_HQ_FAILED",
                    "message": str(ex),
                    "hint": "检查网络或新浪接口是否变更",
                }
            )

    return out, errors


def fetch_market_data(overseas_stub_enabled: bool = False) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """拉取行情区块（与 ``fetch_market_section`` 等价，便于脚本/文档统一命名）。"""
    return fetch_market_section(overseas_stub_enabled)


def fetch_market_sentiment() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """
    模块 1（V2 平滑替换版）：市场情绪热点探针
    - 主数据源 1：ak.stock_hot_tgb() 淘股吧热门帖子 → 提取标题作为热词（自带高频热词）
    - 主数据源 2：ak.stock_hot_rank_wc() 同花顺问财人气股 → Top3 人气股
    - 东财接口降级为兜底备选（云服务器 IP 限制时自动跳过）
    - 所有接口独立容灾，失败不阻塞主流程，返回结构保持不变
    """
    import re  # 内置模块，无需 try-except

    errors: list[dict[str, Any]] = []
    hot_keywords: list[str] = []
    top_hot_stocks: list[dict[str, str]] = []

    try:
        import akshare as ak  # noqa: PLC0415
    except ImportError as e:
        errors.append(
            {
                "source": "market",
                "stage": "market_sentiment_import",
                "code": "AKSHARE_IMPORT_ERROR",
                "message": str(e),
                "hint": "无法导入 AkShare，市场情绪探针不可用",
            }
        )
        return {"hot_keywords": [], "top_hot_stocks": []}, errors

    # ========== 主数据源 1：淘股吧热帖（优先级 > 东财热词）==========
    try:
        df_tgb = ak.stock_hot_tgb()
        if not df_tgb.empty and "标题" in df_tgb.columns:
            raw_titles = df_tgb["标题"].head(5).tolist()
            cleaned_titles = []
            for title in raw_titles:
                title_str = str(title).strip()
                # 基础清洗：去除前后缀标记，截断超长文本
                title_str = re.sub(r"^\d+\s*", "", title_str)  # 去除开头数字序号
                title_str = re.sub(r"\s*\d+\s*$", "", title_str)  # 去除尾部点击量数字
                title_str = title_str.replace("股吧", "").replace("淘股吧", "")  # 去除平台名水印
                title_str = title_str.strip()

                # 截断超长标题，保证可读性
                if len(title_str) > 25:
                    title_str = title_str[:25] + "…"

                if title_str and len(title_str) >= 4:  # 过滤掉过短无意义内容
                    cleaned_titles.append(title_str)

            hot_keywords = cleaned_titles[:5]
    except Exception as e:  # noqa: BLE001
        errors.append(
            {
                "source": "market",
                "stage": "market_sentiment_tgb",
                "code": "TGB_HOT_FAILED",
                "message": repr(e),
                "hint": "淘股吧热帖获取失败，将继续尝试东财接口作为兜底",
            }
        )
        hot_keywords = []

    # ========== 东财热词：降级为兜底备选（云服务器 IP 限制时自动跳过） ==========
    if not hot_keywords:  # 淘股吧失败了才尝试东财
        try:
            df_keywords = ak.stock_hot_keyword_em()
            if not df_keywords.empty and "关键词" in df_keywords.columns:
                hot_keywords = [
                    str(k).strip()
                    for k in df_keywords["关键词"].head(5).tolist()
                    if str(k).strip()
                ]
        except Exception as e:  # noqa: BLE001
            errors.append(
                {
                    "source": "market",
                    "stage": "market_sentiment_em_fallback",
                    "code": "EM_KEYWORDS_FALLBACK_SKIPPED",
                    "message": repr(e),
                    "hint": "东财热词接口 IP 受限，已静默跳过，无需处理",
                }
            )

    # ========== 主数据源 2：同花顺问财人气股（优先级 > 东财人气股）==========
    try:
        df_wc = ak.stock_hot_rank_wc()
        # 兼容同花顺问财不同版本的返回字段（常见字段组合）
        code_col = None
        name_col = None
        for col_name in ["代码", "股票代码", "code", "Code"]:
            if col_name in df_wc.columns:
                code_col = col_name
                break
        for col_name in ["名称", "股票简称", "name", "Name"]:
            if col_name in df_wc.columns:
                name_col = col_name
                break

        if not df_wc.empty and code_col and name_col:
            for _, row in df_wc.head(3).iterrows():
                code = str(row.get(code_col, "") or "").strip()
                name = str(row.get(name_col, "") or "").strip()
                # 清洗：去除代码前后的非数字字符，名称去水印
                code = re.sub(r"\D", "", code)[:6]
                name = name.replace("同花顺", "").replace("问财", "").strip()
                if code and name:
                    top_hot_stocks.append({"name": name, "code": code})
    except Exception as e:  # noqa: BLE001
        errors.append(
            {
                "source": "market",
                "stage": "market_sentiment_wc",
                "code": "WC_RANK_FAILED",
                "message": repr(e),
                "hint": "同花顺问财人气股获取失败，将继续尝试东财接口作为兜底",
            }
        )
        top_hot_stocks = []

    # ========== 东财人气股：降级为兜底备选 ==========
    if not top_hot_stocks:  # 同花顺失败了才尝试东财
        try:
            df_rank = ak.stock_hot_rank_em()
            if not df_rank.empty and "代码" in df_rank.columns and "名称" in df_rank.columns:
                for _, row in df_rank.head(3).iterrows():
                    code = str(row.get("代码", "") or "").strip()
                    name = str(row.get("名称", "") or "").strip()
                    if code and name:
                        top_hot_stocks.append({"name": name, "code": code})
        except Exception as e:  # noqa: BLE001
            errors.append(
                {
                    "source": "market",
                    "stage": "market_sentiment_em_rank_fallback",
                    "code": "EM_RANK_FALLBACK_SKIPPED",
                    "message": repr(e),
                    "hint": "东财人气股接口 IP 受限，已静默跳过",
                }
            )

    return {
        "hot_keywords": hot_keywords,
        "top_hot_stocks": top_hot_stocks,
    }, errors


def extract_keywords_from_news(news_items: list[dict[str, Any]]) -> list[str]:
    """
    终极兜底方案（零网络依赖）：从财联社快讯标题中离线提取高频热词
    设计目标：所有外部接口都挂掉时，依然能给用户展示有意义的内容
    """
    import re

    # 常见股票术语词库（精准匹配，无需分词）
    STOCK_KEYWORDS = [
        "半导体", "芯片", "人工智能", "AI", "算力", "PCB", "半导体设备",
        "新能源", "光伏", "储能", "锂电池", "新能源车", "汽车",
        "军工", "航天", "卫星", "导弹", "船舶",
        "医药", "医疗", "创新药", "CXO", "疫苗", "生物",
        "消费", "白酒", "食品", "零售", "餐饮", "旅游",
        "银行", "券商", "保险", "金融", "地产", "基建",
        "有色", "稀土", "黄金", "铜", "铝", "钢铁",
        "煤炭", "石油", "天然气", "化工",
        "数字经济", "信创", "数据要素", "东数西算",
        "中字头", "国企改革", "央企",
        "科创板", "创业板", "北交所", "主板",
        "北向资金", "外资", "主力资金", "成交额", "成交量",
        "涨停", "跌停", "大涨", "暴跌", "反弹", "反转", "主升浪",
        "业绩", "财报", "净利润", "营收", "盈利", "亏损",
        "集采", "政策", "监管", "利好", "利空",
    ]

    all_text = ""
    for item in news_items:
        title = str(item.get("title") or "").strip()
        clean_text = str(item.get("clean_text") or "").strip()
        all_text += " " + title + " " + clean_text

    matched_keywords = []
    seen = set()
    for kw in STOCK_KEYWORDS:
        pattern = r"(?<![\u4e00-\u9fa5])" + re.escape(kw) + r"(?![\u4e00-\u9fa5])"
        if re.search(pattern, all_text, re.IGNORECASE) and kw not in seen:
            matched_keywords.append(kw)
            seen.add(kw)
        if len(matched_keywords) >= 5:
            break

    # ========== 如果专业术语匹配不足，提取新闻首段主题 ==========
    if len(matched_keywords) < 3:
        for item in news_items[:4]:
            title = str(item.get("title") or "").strip()
            if not title:
                continue

            # 提取标题中的核心短语（去掉时间、数字等前缀）
            clean_title = re.sub(r"^[\d\s\-:：.]+", "", title)
            clean_title = re.sub(r"【.*?】", "", clean_title)
            clean_title = clean_title.strip()

            # 优先提取专业术语（从标题开头找）
            found_kw = None
            for kw in STOCK_KEYWORDS:
                if kw in clean_title and kw not in seen:
                    found_kw = kw
                    break
            if found_kw:
                matched_keywords.append(found_kw)
                seen.add(found_kw)
                if len(matched_keywords) >= 5:
                    break

            # 否则提取干净的主题短语（最多 10 字）
            elif len(clean_title) >= 4 and len(clean_title) <= 12 and clean_title not in seen:
                matched_keywords.append(clean_title)
                seen.add(clean_title)
                if len(matched_keywords) >= 5:
                    break

    return matched_keywords[:5]
