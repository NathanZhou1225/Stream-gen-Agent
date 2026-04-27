#!/usr/bin/env python3
"""拉取当日市场数据：国内大盘 + 资金 + 板块（Tushare），海外 + VIX + 大宗（新浪财经）。

设计原则：
- 每一项数据独立 try/except，单项失败不拖累整体
- 无 TUSHARE_TOKEN → 国内数据段整体降级，海外段仍尝试
- 无网络 → 返回 ok=true + errors 数组 + 空数据，不崩

使用：
    python3 fetch_market.py --json
    python3 fetch_market.py --date 20260421 --json
    python3 fetch_market.py --skip-overseas --json

环境变量：
    TUSHARE_TOKEN  国内数据必填；未设置 → a_share / northbound / industry 三项标记 config_missing

输出结构见文件底部 SAMPLE_OUTPUT。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, Callable
from urllib import request as _urlreq
from urllib.error import URLError

from _common import CST, emit_json, now_iso, today_date_str


A_SHARE_INDICES = [
    {"code": "000001.SH", "name": "上证指数"},
    {"code": "399001.SZ", "name": "深证成指"},
    {"code": "399006.SZ", "name": "创业板指"},
]

OVERSEAS_INDICES = [
    {"sina_code": "gb_$dji", "name": "道琼斯"},
    {"sina_code": "gb_$inx", "name": "标普500"},
    {"sina_code": "gb_$ixic", "name": "纳斯达克"},
]

VIX_CODE = "gb_vxx"  # 新浪已不直接提供 VIX 指数，用 VXX ETF 作代理（高相关但不等同）

COMMODITIES = [
    {"sina_code": "hf_GC", "name": "COMEX 黄金"},
    {"sina_code": "hf_CL", "name": "WTI 原油"},
]


# ---------- 小工具 ----------

def _to_tushare_date(date_str: str) -> str:
    """--date 支持 YYYY-MM-DD 或 YYYYMMDD，统一转 YYYYMMDD（Tushare 要求）。"""
    s = date_str.replace("-", "")
    if len(s) != 8 or not s.isdigit():
        raise ValueError(f"日期格式应为 YYYY-MM-DD 或 YYYYMMDD，实际收到：{date_str}")
    return s


def _try(result: dict[str, Any], key: str, fn: Callable[[], Any]) -> None:
    """统一 try/except 包装，失败记到 errors 里，不传播。"""
    try:
        result["data"][key] = fn()
    except Exception as e:  # noqa: BLE001
        result["data"][key] = None
        result["errors"].append({
            "item": key,
            "reason": f"{type(e).__name__}: {e}",
        })


# ---------- 国内：Tushare ----------

def _require_tushare_client(token: str):
    """延迟 import tushare，避免用户未装时影响其他逻辑。"""
    try:
        import tushare as ts
    except ImportError as e:
        raise RuntimeError(
            "未安装 tushare，请运行 pip install tushare（或参考 requirements.txt）"
        ) from e
    return ts.pro_api(token)


def fetch_a_share_indices(token: str, trade_date: str) -> dict[str, Any]:
    pro = _require_tushare_client(token)
    items = []
    for cfg in A_SHARE_INDICES:
        df = pro.index_daily(ts_code=cfg["code"], start_date=trade_date, end_date=trade_date)
        if df is None or df.empty:
            items.append({
                "code": cfg["code"], "name": cfg["name"],
                "close": None, "pct_change": None, "amount_yi": None,
                "note": f"Tushare 无 {trade_date} 数据（可能非交易日或接口权限不足）",
            })
            continue
        row = df.iloc[0]
        items.append({
            "code": cfg["code"],
            "name": cfg["name"],
            "close": round(float(row["close"]), 2),
            "pct_change": round(float(row["pct_chg"]), 2),
            # Tushare amount 单位：千元，换算成亿：/ 1e5
            "amount_yi": round(float(row["amount"]) / 1e5, 1),
        })
    return {
        "trade_date": trade_date,
        "source": "tushare:index_daily",
        "items": items,
    }


def fetch_northbound_flow(token: str, trade_date: str) -> dict[str, Any]:
    pro = _require_tushare_client(token)
    # moneyflow_hsgt: 沪深港通资金流向（单位：万元）
    df = pro.moneyflow_hsgt(start_date=trade_date, end_date=trade_date)
    if df is None or df.empty:
        return {
            "trade_date": trade_date,
            "source": "tushare:moneyflow_hsgt",
            "north_net_yi": None,
            "south_net_yi": None,
            "note": f"无 {trade_date} 数据或接口权限不足",
        }
    row = df.iloc[0]
    north_net = (
        float(row.get("north_money", 0) or 0) / 1e4
    )  # 万元 → 亿元
    south_net = (
        float(row.get("south_money", 0) or 0) / 1e4
    )
    return {
        "trade_date": trade_date,
        "source": "tushare:moneyflow_hsgt",
        "north_net_yi": round(north_net, 2),
        "south_net_yi": round(south_net, 2),
    }


def fetch_industry_top_gainers(token: str, trade_date: str, top: int = 3) -> dict[str, Any]:
    """申万一级行业当日涨幅 Top N。

    说明：Tushare 免费账户通常无法访问申万行业指数历史数据。
    这里尝试 index_classify + index_daily 组合；失败时抛出交给 _try 降级。
    """
    pro = _require_tushare_client(token)
    # 拿申万一级分类（sw_l1 约 31 个行业）
    classify = pro.index_classify(level="L1", src="SW2021")
    if classify is None or classify.empty:
        raise RuntimeError("index_classify 返回空；积分可能不足 / 免费账户不支持")

    gainers = []
    # 为了避免大量 API 调用，只取前 15 个行业各问一次（防积分耗尽）
    for _, ind in classify.head(15).iterrows():
        try:
            d = pro.sw_daily(ts_code=ind["index_code"], start_date=trade_date, end_date=trade_date)
        except Exception:  # noqa: BLE001 某些 tushare 版本 sw_daily 要权限
            d = None
        if d is None or d.empty:
            continue
        r = d.iloc[0]
        gainers.append({
            "name": ind.get("industry_name") or ind.get("name"),
            "code": ind["index_code"],
            "pct_change": round(float(r["pct_change"]), 2),
        })

    if not gainers:
        raise RuntimeError("sw_daily 全部返回空；免费账户通常无此权限，建议升级或跳过")

    gainers.sort(key=lambda x: x["pct_change"], reverse=True)
    return {
        "trade_date": trade_date,
        "source": "tushare:sw_daily",
        "items": gainers[:top],
    }


# ---------- 海外：新浪财经（免 key） ----------

def _sina_fetch(codes: list[str]) -> dict[str, list[str]]:
    """批量拉取新浪行情。codes 例：['gb_$dji', 'hf_GC']。

    返回：{code: [field1, field2, ...]}
    """
    url = "http://hq.sinajs.cn/list=" + ",".join(codes)
    req = _urlreq.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) streamy-content-gen/0.1",
            "Referer": "https://finance.sina.com.cn/",
        },
    )
    with _urlreq.urlopen(req, timeout=6) as resp:
        # 新浪返回 GBK 编码
        raw = resp.read().decode("gbk", errors="replace")

    out: dict[str, list[str]] = {}
    for line in raw.strip().splitlines():
        # var hq_str_gb_$dji="道琼斯,38671.69,..."
        if "=" not in line:
            continue
        left, right = line.split("=", 1)
        code = left.replace("var hq_str_", "").strip()
        payload = right.strip().strip('";').strip('"')
        out[code] = [x.strip() for x in payload.split(",")]
    return out


def fetch_overseas_indices_and_vix() -> dict[str, Any]:
    codes = [cfg["sina_code"] for cfg in OVERSEAS_INDICES] + [VIX_CODE]
    raw = _sina_fetch(codes)

    # 新浪美股实际字段（验证过 2026-04）：
    # [0] name_zh   [1] now        [2] change_pct   [3] datetime
    # [4] change_abs [5] prev_close [6] high        [7] low
    us_indices = []
    for cfg in OVERSEAS_INDICES:
        data = raw.get(cfg["sina_code"], [])
        if len(data) < 6:
            us_indices.append({
                "code": cfg["sina_code"], "name": cfg["name"],
                "close": None, "pct_change": None,
                "note": "新浪接口返回不完整",
            })
            continue
        us_indices.append({
            "code": cfg["sina_code"],
            "name": cfg["name"],
            "close": _safe_float(data[1]),
            "pct_change": _safe_float(data[2]),
            "change_abs": _safe_float(data[4]),
            "prev_close": _safe_float(data[5]),
        })

    # VIX 代理：VXX ETF（新浪已不直接提供 VIX）
    vix_data = raw.get(VIX_CODE, [])
    if len(vix_data) >= 6:
        vix = {
            "value": _safe_float(vix_data[1]),
            "pct_change": _safe_float(vix_data[2]),
            "source": "sina:gb_vxx",
            "note": "VXX ETF 作为 VIX 代理，方向性一致但绝对值不同",
        }
    else:
        vix = {
            "value": None, "pct_change": None, "source": "sina:gb_vxx",
            "note": "VXX 数据获取失败",
        }

    return {
        "source": "sina:hq.sinajs.cn",
        "us_indices": us_indices,
        "vix": vix,
    }


def fetch_commodities() -> dict[str, Any]:
    codes = [c["sina_code"] for c in COMMODITIES]
    raw = _sina_fetch(codes)
    items = []
    for cfg in COMMODITIES:
        data = raw.get(cfg["sina_code"], [])
        if len(data) < 8:
            items.append({
                "code": cfg["sina_code"], "name": cfg["name"],
                "close": None, "pct_change": None,
                "note": "新浪接口返回不完整",
            })
            continue
        # hf_GC 字段顺序（不同类型商品略有差异，取常用字段）：
        # [now, ask_bid(ignored), bid(ignored), high, low, prev, open, close_yesterday, pct?, ..., name_zh]
        # 保守：用 data[0] 当 now，数组末尾字段里找符合的 pct；鲁棒做法：算 (now-prev)/prev
        now = _safe_float(data[0])
        prev = None
        for field in data[5:9]:
            if field and _safe_float(field) is not None:
                prev = _safe_float(field)
                break
        pct = None
        if now is not None and prev is not None and prev != 0:
            pct = round((now - prev) / prev * 100, 2)
        items.append({
            "code": cfg["sina_code"],
            "name": cfg["name"],
            "close": now,
            "pct_change": pct,
        })
    return {
        "source": "sina:hq.sinajs.cn",
        "items": items,
    }


def _safe_float(s: Any) -> float | None:
    try:
        return round(float(s), 2)
    except (TypeError, ValueError):
        return None


# ---------- 主流程 ----------

def run(date: str, skip_overseas: bool = False) -> dict[str, Any]:
    trade_date = _to_tushare_date(date)
    token = os.environ.get("TUSHARE_TOKEN", "").strip()

    result: dict[str, Any] = {
        "ok": True,
        "command": "fetch_market",
        "as_of": now_iso(),
        "trade_date": trade_date,
        "data": {
            "a_share_indices": None,
            "northbound_flow": None,
            "industry_top_gainers": None,
            "overseas": None,
            "commodities": None,
        },
        "errors": [],
    }

    if not token:
        result["errors"].append({
            "item": "tushare",
            "reason": "TUSHARE_TOKEN_MISSING",
            "hint": "v1 降级：国内数据未获取，请走 BYOD 或设置 TUSHARE_TOKEN 后重试",
        })
    else:
        _try(result, "a_share_indices", lambda: fetch_a_share_indices(token, trade_date))
        _try(result, "northbound_flow", lambda: fetch_northbound_flow(token, trade_date))
        _try(result, "industry_top_gainers", lambda: fetch_industry_top_gainers(token, trade_date))

    if not skip_overseas:
        _try(result, "overseas", fetch_overseas_indices_and_vix)
        _try(result, "commodities", fetch_commodities)

    # 生成 summary（给 Agent 一眼看完 + 方便 prompt 里 {context} 插入）
    result["summary"] = _build_summary(result)
    return result


def _build_summary(result: dict[str, Any]) -> str:
    lines = [f"市场速览（trade_date={result['trade_date']}）："]
    a = (result["data"].get("a_share_indices") or {}).get("items") or []
    if a:
        lines.append("• A 股: " + " / ".join(
            f"{x['name']}{x['close']}({'+' if (x.get('pct_change') or 0) >= 0 else ''}{x.get('pct_change')}%)"
            for x in a if x.get("close") is not None
        ))
    nb = result["data"].get("northbound_flow") or {}
    if nb.get("north_net_yi") is not None:
        lines.append(f"• 北上资金净买入：{nb['north_net_yi']} 亿")
    ind = (result["data"].get("industry_top_gainers") or {}).get("items") or []
    if ind:
        lines.append("• 领涨行业: " + ", ".join(f"{x['name']}({x['pct_change']}%)" for x in ind))
    ov = result["data"].get("overseas") or {}
    us = ov.get("us_indices") or []
    vix = ov.get("vix") or {}
    if us:
        lines.append("• 美股隔夜: " + " / ".join(
            f"{x['name']}{'+' if (x.get('pct_change') or 0) >= 0 else ''}{x.get('pct_change')}%"
            for x in us if x.get("pct_change") is not None
        ))
    if vix.get("value") is not None:
        lines.append(f"• VIX: {vix['value']}（{'+' if (vix.get('pct_change') or 0) >= 0 else ''}{vix.get('pct_change')}%)")
    com = (result["data"].get("commodities") or {}).get("items") or []
    if com:
        lines.append("• 大宗: " + " / ".join(
            f"{x['name']}{x.get('close')}({'+' if (x.get('pct_change') or 0) >= 0 else ''}{x.get('pct_change')}%)"
            for x in com if x.get("close") is not None
        ))
    if result["errors"]:
        lines.append(f"⚠️  {len(result['errors'])} 项数据获取失败（见 errors 字段）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="市场数据拉取（Tushare + 新浪财经）")
    parser.add_argument("--date", default=today_date_str(), help="日期 YYYY-MM-DD 或 YYYYMMDD，默认今天")
    parser.add_argument("--skip-overseas", action="store_true", help="跳过海外数据（只拉国内）")
    parser.add_argument("--json", action="store_true", help="兼容性开关（默认 JSON 输出）")
    args = parser.parse_args(argv)

    result = run(date=args.date, skip_overseas=args.skip_overseas)
    emit_json(result)


if __name__ == "__main__":
    main(sys.argv[1:])


# ---------- 文档：输出样例 ----------
SAMPLE_OUTPUT = """
{
  "ok": true,
  "command": "fetch_market",
  "as_of": "2026-04-21T17:45:00+08:00",
  "trade_date": "20260421",
  "data": {
    "a_share_indices": {
      "trade_date": "20260421",
      "source": "tushare:index_daily",
      "items": [
        {"code": "000001.SH", "name": "上证指数", "close": 3428.12, "pct_change": 0.82, "amount_yi": 4200.5}
      ]
    },
    "northbound_flow": {"trade_date": "20260421", "source": "tushare:moneyflow_hsgt",
                        "north_net_yi": 127.5, "south_net_yi": 38.2},
    "industry_top_gainers": {"trade_date": "20260421", "source": "tushare:sw_daily",
                              "items": [{"name": "AI算力", "code": "...", "pct_change": 3.5}]},
    "overseas": {
      "source": "sina:hq.sinajs.cn",
      "us_indices": [{"code": "gb_$dji", "name": "道琼斯", "close": 38671.69, "pct_change": 0.28}],
      "vix": {"value": 13.2, "pct_change": -1.5, "source": "sina:gb_vix"}
    },
    "commodities": {
      "source": "sina:hq.sinajs.cn",
      "items": [{"code": "hf_GC", "name": "COMEX 黄金", "close": 2352.3, "pct_change": 0.4}]
    }
  },
  "errors": [],
  "summary": "市场速览..."
}
"""
