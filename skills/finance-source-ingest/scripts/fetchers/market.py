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
HK_SINA_INDEX_SPECS: list[dict[str, str]] = [
    {"sina_code": "rt_hkHSI", "name": "恒生指数", "code": "HSI"},
    {"sina_code": "rt_hkHSTECH", "name": "恒生科技指数", "code": "HSTECH"},
    {"sina_code": "rt_hkHSCEI", "name": "恒生中国企业指数", "code": "HSCEI"},
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


def _fetch_hk_indices_from_sina(*, timeout: float = 8.0) -> list[dict[str, Any]]:
    raw = _sina_fetch([s["sina_code"] for s in HK_SINA_INDEX_SPECS], timeout=timeout)
    items: list[dict[str, Any]] = []
    for spec in HK_SINA_INDEX_SPECS:
        payload = raw.get(spec["sina_code"], [])
        name = payload[1] if len(payload) > 1 and payload[1] else spec["name"]
        close = _to_float(payload[6]) if len(payload) > 6 else None
        pct = _to_float(payload[8]) if len(payload) > 8 else None
        items.append(
            normalize_index_item(
                code=spec["code"],
                name=str(name),
                close=close,
                pct_change=pct,
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


def _skip_akshare_probe() -> bool:
    """设为 1 时跳过东财/AkShare 探测（仅新浪指数），用于极端离线/WAF 环境。"""
    return os.environ.get("FINANCE_SOURCE_SKIP_AKSHARE_PROBE", "0").strip() == "1"


def _akshare_probe_timeout_sec() -> float:
    raw = os.environ.get("FINANCE_SOURCE_AKSHARE_PROBE_TIMEOUT", "14").strip() or "14"
    try:
        t = float(raw)
    except ValueError:
        return 14.0
    return max(3.0, min(t, 45.0))


def _ak_pool_call(fn: Callable[[], Any], *, timeout_sec: float) -> Any:
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(fn)
        return fut.result(timeout=timeout_sec)


def _probe_northbound_hsgt(ak: Any, errors: list[dict[str, Any]]) -> dict[str, Any] | None:
    import pandas as pd  # noqa: PLC0415

    fn = getattr(ak, "stock_hsgt_fund_flow_summary_em", None)
    if not callable(fn):
        return None
    try:
        df = _ak_pool_call(fn, timeout_sec=_akshare_probe_timeout_sec())
    except Exception as e:  # noqa: BLE001
        errors.append(
            {
                "source": "market",
                "stage": "northbound_probe",
                "code": "NORTHBOUND_PROBE_FAILED",
                "message": repr(e),
                "hint": "AkShare stock_hsgt_fund_flow_summary_em",
            }
        )
        return None
    if df is None or getattr(df, "empty", True):
        return None
    if "资金方向" not in df.columns or "板块" not in df.columns:
        return None
    work = df[(df["资金方向"] == "北向") & (df["板块"].isin(["沪股通", "深股通"]))]
    if work.empty:
        return None
    col = "资金净流入" if "资金净流入" in work.columns else None
    if not col:
        return None
    total = pd.to_numeric(work[col], errors="coerce").sum()
    if pd.isna(total):
        return None
    return {
        "source": "akshare:stock_hsgt_fund_flow_summary_em",
        "aggregate_net_buy_yi": float(round(float(total), 4)),
        "note": "北向资金为沪股通+深股通「资金净流入」行汇总（AkShare 东财口径）",
    }


def _probe_industry_rank(ak: Any, errors: list[dict[str, Any]]) -> dict[str, Any] | None:
    fn = getattr(ak, "stock_fund_flow_industry", None)
    if not callable(fn):
        return None
    try:
        df = _ak_pool_call(
            lambda: _call_with_retries(lambda: fn(), attempts=2),
            timeout_sec=_akshare_probe_timeout_sec(),
        )
    except Exception as e:  # noqa: BLE001
        errors.append(
            {
                "source": "market",
                "stage": "industry_rank_probe",
                "code": "INDUSTRY_RANK_PROBE_FAILED",
                "message": repr(e),
                "hint": "AkShare stock_fund_flow_industry",
            }
        )
        return None
    if df is None or getattr(df, "empty", True):
        return None
    name_col = "行业" if "行业" in df.columns else None
    pct_col = "行业-涨跌幅" if "行业-涨跌幅" in df.columns else None
    if not name_col or not pct_col:
        return None
    items: list[dict[str, Any]] = []
    for _, row in df.head(8).iterrows():
        items.append(
            {
                "name": str(row.get(name_col, "") or "").strip(),
                "pct_change": _to_float(row.get(pct_col)),
            }
        )
    return {"source": "akshare:stock_fund_flow_industry", "items": items}


def _probe_market_temperature(ak: Any, errors: list[dict[str, Any]]) -> dict[str, Any]:
    """涨跌停池 + 行业主力净流入 Top（尽力而为，失败保留字段与 None）。"""
    import pandas as pd  # noqa: PLC0415

    out: dict[str, Any] = {
        "source": "akshare:zt_dt_industry_probe",
        "top_inflow_sectors": [],
        "limit_up_count": None,
        "limit_down_count": None,
    }
    retries = max(1, min(_akshare_retry_count(), 4))
    for fn_name in ("stock_zt_pool_em", "stock_dt_pool_em"):
        fn = getattr(ak, fn_name, None)
        if not callable(fn):
            continue
        try:
            df = _ak_pool_call(
                lambda f=fn: _call_with_retries(lambda: f(), attempts=retries),
                timeout_sec=_akshare_probe_timeout_sec(),
            )
            n = int(len(df)) if df is not None and not getattr(df, "empty", True) else 0
            if fn_name == "stock_zt_pool_em":
                out["limit_up_count"] = n
            else:
                out["limit_down_count"] = n
        except Exception as e:  # noqa: BLE001
            errors.append(
                {
                    "source": "market",
                    "stage": fn_name,
                    "code": "TEMPERATURE_POOL_PROBE_FAILED",
                    "message": repr(e),
                    "hint": fn_name,
                }
            )
    ff = getattr(ak, "stock_fund_flow_industry", None)
    if callable(ff):
        try:
            df2 = _ak_pool_call(
                lambda: _call_with_retries(lambda: ff(), attempts=2),
                timeout_sec=_akshare_probe_timeout_sec(),
            )
            if df2 is not None and not getattr(df2, "empty", True):
                net_col = "净额" if "净额" in df2.columns else None
                name_col = "行业" if "行业" in df2.columns else None
                if net_col and name_col:
                    work = df2.copy()
                    work["_n"] = pd.to_numeric(work[net_col], errors="coerce")
                    work = work.dropna(subset=["_n"]).sort_values("_n", ascending=False).head(3)
                    secs: list[dict[str, Any]] = []
                    for _, row in work.iterrows():
                        nm = str(row.get(name_col, "") or "").strip()
                        if nm:
                            secs.append({"name": nm, "main_net_inflow_yi": _to_float(row.get(net_col))})
                    out["top_inflow_sectors"] = secs
        except Exception as e:  # noqa: BLE001
            errors.append(
                {
                    "source": "market",
                    "stage": "temperature_fund_flow",
                    "code": "INFLOW_SECTOR_PROBE_FAILED",
                    "message": repr(e),
                    "hint": "stock_fund_flow_industry",
                }
            )
    return out


def _fill_akshare_market_extensions(out: dict[str, Any], errors: list[dict[str, Any]]) -> None:
    """在新浪三大指数就绪后，尽力探测北向/行业/涨跌停（失败不抛异常，只记 errors）。"""
    if _skip_akshare_probe():
        errors.append(
            {
                "source": "market",
                "stage": "akshare_probe",
                "code": "AKSHARE_PROBE_SKIPPED",
                "message": "FINANCE_SOURCE_SKIP_AKSHARE_PROBE=1，已跳过东财/AkShare 扩展探测",
                "hint": "需要北向/行业/涨跌停时请 unset 该变量",
            }
        )
        return
    try:
        import akshare as ak  # noqa: PLC0415
    except ImportError as e:
        errors.append(
            {
                "source": "market",
                "stage": "akshare_probe",
                "code": "AKSHARE_IMPORT_ERROR",
                "message": str(e),
                "hint": "无法 import akshare，扩展行情字段保持为空",
            }
        )
        return
    nb = _probe_northbound_hsgt(ak, errors)
    if nb:
        out["northbound"] = nb
    ir = _probe_industry_rank(ak, errors)
    if ir:
        out["industry_rank"] = ir
    out["market_temperature"] = _probe_market_temperature(ak, errors)


def fetch_market_section(overseas_stub_enabled: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """
    行情：新浪三大指数（主路径）+ **尽力** AkShare 探测北向/行业/涨跌停（可 `FINANCE_SOURCE_SKIP_AKSHARE_PROBE=1` 全关）。
    """
    errors: list[dict[str, Any]] = []
    out: dict[str, Any] = {
        "source_primary": "sina_native+akshare_probe",
        "as_of": now_iso(),
        "a_share_indices": None,
        "hong_kong_indices": None,
        "northbound": None,
        "industry_rank": None,
        "market_temperature": {
            "source": "pending",
            "top_inflow_sectors": [],
            "limit_up_count": None,
            "limit_down_count": None,
        },
        "overseas_stub": None,
    }

    try:
        fb = _fetch_a_share_indices_from_sina_trinity(timeout=10.0)
        if _fallback_indices_usable(fb):
            out["a_share_indices"] = {
                "source": "sina:hq.sinajs.cn,list=s_sh000001,s_sz399001,s_sz399006",
                "items": fb,
            }
        else:
            out["a_share_indices"] = {
                "source": "sina:placeholder",
                "items": _placeholder_trinity_items(),
            }
            errors.append(
                {
                    "source": "market",
                    "stage": "a_share_indices",
                    "code": "SINA_TRINITY_EMPTY",
                    "message": "新浪三大指数无有效收盘价",
                    "hint": "检查出网或新浪接口是否变更",
                }
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("新浪三大指数失败: %s", repr(e))
        out["a_share_indices"] = {
            "source": "sina:placeholder",
            "items": _placeholder_trinity_items(),
        }
        errors.append(
            {
                "source": "market",
                "stage": "a_share_indices_sina_fallback",
                "code": "SINA_TRINITY_FAILED",
                "message": repr(e),
                "hint": "urllib 请求新浪 list 接口失败",
            }
        )

    try:
        hk_items = _fetch_hk_indices_from_sina(timeout=8.0)
        if any(x.get("close") is not None for x in hk_items):
            out["hong_kong_indices"] = {
                "source": "sina:hq.sinajs.cn,list=rt_hkHSI,rt_hkHSTECH,rt_hkHSCEI",
                "items": hk_items,
            }
    except Exception as e:  # noqa: BLE001
        logger.warning("新浪港股指数失败: %s", repr(e))
        errors.append(
            {
                "source": "market",
                "stage": "hong_kong_indices_sina",
                "code": "SINA_HK_INDICES_FAILED",
                "message": repr(e),
                "hint": "urllib 请求新浪港股指数失败",
            }
        )

    _fill_akshare_market_extensions(out, errors)

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


def fetch_market_temperature() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """独立入口：与 ``fetch_market_section`` 内探测逻辑一致（供单测/脚本）。"""
    errors: list[dict[str, Any]] = []
    if _skip_akshare_probe():
        return (
            {
                "source": "skipped:FINANCE_SOURCE_SKIP_AKSHARE_PROBE",
                "top_inflow_sectors": [],
                "limit_up_count": None,
                "limit_down_count": None,
            },
            errors,
        )
    try:
        import akshare as ak  # noqa: PLC0415
    except ImportError as e:
        errors.append({"source": "market", "stage": "temperature_import", "code": "AKSHARE_IMPORT_ERROR", "message": str(e)})
        return (
            {"source": "none", "top_inflow_sectors": [], "limit_up_count": None, "limit_down_count": None},
            errors,
        )
    return _probe_market_temperature(ak, errors), errors


def fetch_market_sentiment() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """市场情绪：依次尝试淘股吧、同花顺问财、东财人气/热词（单项失败不阻塞）。"""
    import re

    errors: list[dict[str, Any]] = []
    out: dict[str, Any] = {"hot_keywords": [], "top_hot_stocks": [], "disabled": False, "note": ""}
    if _skip_akshare_probe():
        out["note"] = "FINANCE_SOURCE_SKIP_AKSHARE_PROBE=1，跳过情绪热榜探测"
        return out, errors
    try:
        import akshare as ak  # noqa: PLC0415
    except ImportError as e:
        errors.append({"source": "market", "stage": "sentiment_import", "code": "AKSHARE_IMPORT_ERROR", "message": str(e)})
        out["note"] = "akshare 不可用"
        return out, errors

    hot_keywords: list[str] = []
    top_hot_stocks: list[dict[str, str]] = []

    try:
        tgb_fn = getattr(ak, "stock_hot_tgb", None)
        if not callable(tgb_fn):
            raise AttributeError("akshare has no stock_hot_tgb")
        df_tgb = _ak_pool_call(lambda: tgb_fn(), timeout_sec=_akshare_probe_timeout_sec())
        if df_tgb is not None and not getattr(df_tgb, "empty", True) and "标题" in df_tgb.columns:
            for title in df_tgb["标题"].head(5).tolist():
                title_str = str(title).strip()
                title_str = re.sub(r"^\d+\s*", "", title_str)
                title_str = re.sub(r"\s*\d+\s*$", "", title_str)
                title_str = title_str.replace("股吧", "").replace("淘股吧", "").strip()
                if len(title_str) > 28:
                    title_str = title_str[:28] + "…"
                if title_str and len(title_str) >= 4:
                    hot_keywords.append(title_str)
    except Exception as e:  # noqa: BLE001
        errors.append({"source": "market", "stage": "sentiment_tgb", "code": "TGB_HOT_FAILED", "message": repr(e)})

    if not hot_keywords:
        try:
            kw_fn = getattr(ak, "stock_hot_keyword_em", None)
            if not callable(kw_fn):
                raise AttributeError("akshare has no stock_hot_keyword_em")
            df_kw = _ak_pool_call(lambda: kw_fn(), timeout_sec=_akshare_probe_timeout_sec())
            if df_kw is not None and not getattr(df_kw, "empty", True) and "关键词" in df_kw.columns:
                hot_keywords = [
                    str(k).strip()
                    for k in df_kw["关键词"].head(5).tolist()
                    if str(k).strip()
                ]
        except Exception as e:  # noqa: BLE001
            errors.append({"source": "market", "stage": "sentiment_em_kw", "code": "EM_KEYWORDS_FAILED", "message": repr(e)})

    try:
        wc_fn = getattr(ak, "stock_hot_rank_wc", None)
        if not callable(wc_fn):
            raise AttributeError("akshare has no stock_hot_rank_wc")
        df_wc = _ak_pool_call(lambda: wc_fn(), timeout_sec=_akshare_probe_timeout_sec())
        code_col = name_col = None
        if df_wc is not None and not getattr(df_wc, "empty", True):
            for col_name in ("代码", "股票代码", "code", "Code"):
                if col_name in df_wc.columns:
                    code_col = col_name
                    break
            for col_name in ("名称", "股票简称", "name", "Name"):
                if col_name in df_wc.columns:
                    name_col = col_name
                    break
        if df_wc is not None and code_col and name_col:
            for _, row in df_wc.head(3).iterrows():
                code = re.sub(r"\D", "", str(row.get(code_col, "") or ""))[:6]
                name = str(row.get(name_col, "") or "").strip().replace("同花顺", "").replace("问财", "").strip()
                if code and name:
                    top_hot_stocks.append({"name": name, "code": code})
    except Exception as e:  # noqa: BLE001
        errors.append({"source": "market", "stage": "sentiment_wc", "code": "WC_RANK_FAILED", "message": repr(e)})

    if not top_hot_stocks:
        try:
            rank_fn = getattr(ak, "stock_hot_rank_em", None)
            if not callable(rank_fn):
                raise AttributeError("akshare has no stock_hot_rank_em")
            df_rank = _ak_pool_call(lambda: rank_fn(), timeout_sec=_akshare_probe_timeout_sec())
            if df_rank is not None and not getattr(df_rank, "empty", True) and "代码" in df_rank.columns and "名称" in df_rank.columns:
                for _, row in df_rank.head(3).iterrows():
                    code = str(row.get("代码", "") or "").strip()
                    name = str(row.get("名称", "") or "").strip()
                    if code and name:
                        top_hot_stocks.append({"name": name, "code": code})
        except Exception as e:  # noqa: BLE001
            errors.append({"source": "market", "stage": "sentiment_em_rank", "code": "EM_RANK_FAILED", "message": repr(e)})

    out["hot_keywords"] = hot_keywords[:8]
    out["top_hot_stocks"] = top_hot_stocks[:5]
    if not hot_keywords and not top_hot_stocks:
        out["note"] = "情绪热榜各源均未返回可用数据"
    else:
        out["note"] = ""
    return out, errors


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
