"""梯队三：第三方 JSON API；MVP 无 URL 时走微博热搜 → AkShare 社区热榜多级降级。"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from _common import now_iso

logger = logging.getLogger(__name__)

WB_HOT_URL = "https://api.vvhan.com/api/hotlist/wbHot"
WB_HOT_TIMEOUT_SEC = 3.0

BAIDU_BOARD_URL = "https://top.baidu.com/board?tab=realtime"
BAIDU_TIMEOUT_SEC = 8.0

# 第一梯队：宏观 / 金融向过滤（与产品约定一致）
MACRO_FINANCE_KEYWORDS = ["股", "降息", "央行", "外资", "黄金", "楼市", "经济", "汇率"]


def _social_item(*, title: str, clean_text: str, platform: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "title": title.strip() or (clean_text.strip() or "（无标题）"),
        "clean_text": (clean_text.strip() or title.strip() or "（无正文）"),
    }
    if platform:
        row["platform"] = platform
    return row


def _vvhan_wb_hot_entries(body: Any) -> list[dict[str, str]]:
    """解析 vvhan 微博热搜 JSON，产出 title/hot/url 列表。"""
    if not isinstance(body, dict):
        return []
    data = body.get("data")
    if not isinstance(data, list):
        return []
    out: list[dict[str, str]] = []
    for it in data:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or it.get("word") or it.get("name") or "").strip()
        if not title:
            continue
        hot = it.get("hot") or it.get("num") or it.get("hotValue") or ""
        url = str(it.get("url") or it.get("mobilUrl") or it.get("link") or "").strip()
        out.append({"title": title, "hot": str(hot).strip(), "url": url})
    return out


def _wb_title_matches_macro(title: str) -> bool:
    return any(kw in title for kw in MACRO_FINANCE_KEYWORDS)


def _wb_title_matches_user_keywords(title: str, user_keywords: list[str]) -> bool:
    if not user_keywords:
        return True
    blob = title.lower()
    return any(k.lower() in blob for k in user_keywords)


def _extract_baidu_board_titles(html: str) -> list[str]:
    """从百度实时热搜页提取标题（多策略）。"""
    titles: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(
        r'class="c-single-text-ellipsis[^"]*"[^>]*>([^<]+)</',
        html,
        flags=re.I,
    ):
        t = _clean_title(m.group(1))
        if t and t not in seen and len(t) >= 2:
            seen.add(t)
            titles.append(t)
    if len(titles) < 5:
        for m in re.finditer(r'"word"\s*:\s*"([^"\\]+)"', html):
            t = _clean_title(m.group(1))
            if t and t not in seen and 2 <= len(t) <= 80:
                seen.add(t)
                titles.append(t)
    return titles[:60]


def _clean_title(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _tier3_baidu_hot_filtered(
    user_keywords: list[str],
    max_items: int,
    errors: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """第三梯队：百度实时热搜 HTML + 宏观词过滤（Tier1+2 均不可用或为空时）。"""
    try:
        req = urlrequest.Request(
            BAIDU_BOARD_URL,
            headers={"User-Agent": "Mozilla/5.0 finance-source-ingest/0.3"},
        )
        with urlrequest.urlopen(req, timeout=BAIDU_TIMEOUT_SEC) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except (urlerror.URLError, OSError, TimeoutError, ValueError) as e:
        logger.warning("百度热搜不可用: %s", repr(e))
        errors.append(
            {
                "source": "social",
                "stage": "social_trends_tier3",
                "code": "BAIDU_HOT_FAILED",
                "message": str(e),
                "hint": BAIDU_BOARD_URL,
            },
        )
        return None

    raw_titles = _extract_baidu_board_titles(html)
    if not raw_titles:
        errors.append(
            {
                "source": "social",
                "stage": "social_trends_tier3",
                "code": "BAIDU_HOT_FAILED",
                "message": "页面无可用标题（结构变更）",
                "hint": BAIDU_BOARD_URL,
            },
        )
        return None

    macro_hits = [t for t in raw_titles if _wb_title_matches_macro(t)]
    if not macro_hits:
        errors.append(
            {
                "source": "social",
                "stage": "social_trends_tier3",
                "code": "BAIDU_HOT_NO_MATCH",
                "message": "百度热搜无命中宏观/金融关键词",
                "hint": str(MACRO_FINANCE_KEYWORDS),
            },
        )
        return None

    filtered = [t for t in macro_hits if _wb_title_matches_user_keywords(t, user_keywords)]
    if not filtered:
        errors.append(
            {
                "source": "social",
                "stage": "social_trends_tier3",
                "code": "BAIDU_HOT_NO_USER_KEYWORD_MATCH",
                "message": "百度热搜命中宏观词后，被 CLI --keywords 过滤为空",
            },
        )
        return None

    items: list[dict[str, Any]] = []
    for title in filtered[: max(1, max_items)]:
        clean = f"{title} {BAIDU_BOARD_URL}".strip()
        items.append(_social_item(title=title, clean_text=clean, platform="百度热搜"))
    return items


def _tier1_weibo_hot_filtered(
    user_keywords: list[str],
    max_items: int,
    errors: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """
    第一梯队：vvhan 微博热搜。
    成功且至少 1 条命中宏观词（及可选 CLI keywords）则返回 items；否则返回 None 表示交第二梯队。
    """
    try:
        req = urlrequest.Request(
            WB_HOT_URL,
            headers={"User-Agent": "Mozilla/5.0 finance-source-ingest/0.1"},
        )
        with urlrequest.urlopen(req, timeout=WB_HOT_TIMEOUT_SEC) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        body = json.loads(raw)
    except (urlerror.URLError, OSError, TimeoutError, json.JSONDecodeError, ValueError) as e:
        logger.warning("微博热搜 API 不可用: %s", repr(e))
        errors.append(
            {
                "source": "social",
                "stage": "social_trends_tier1",
                "code": "SOCIAL_WB_HOT_FAILED",
                "message": str(e),
                "hint": WB_HOT_URL,
            }
        )
        return None

    try:
        entries = _vvhan_wb_hot_entries(body)
    except Exception as e:  # noqa: BLE001
        logger.warning("微博热搜 JSON 解析异常: %s", repr(e))
        errors.append(
            {
                "source": "social",
                "stage": "social_trends_tier1",
                "code": "SOCIAL_WB_HOT_PARSE",
                "message": repr(e),
                "hint": "响应非预期结构，将尝试第二梯队",
            }
        )
        return None

    if not entries:
        errors.append(
            {
                "source": "social",
                "stage": "social_trends_tier1",
                "code": "SOCIAL_WB_HOT_EMPTY",
                "message": "微博热搜列表为空",
                "hint": "将尝试第二梯队",
            }
        )
        return None

    macro_hits = [e for e in entries if _wb_title_matches_macro(e["title"])]
    if not macro_hits:
        errors.append(
            {
                "source": "social",
                "stage": "social_trends_tier1",
                "code": "SOCIAL_WB_HOT_NO_MACRO_MATCH",
                "message": "微博热搜无命中宏观/金融关键词",
                "hint": f"关键词集: {MACRO_FINANCE_KEYWORDS}；将尝试第二梯队",
            }
        )
        return None

    filtered = [e for e in macro_hits if _wb_title_matches_user_keywords(e["title"], user_keywords)]
    if not filtered:
        errors.append(
            {
                "source": "social",
                "stage": "social_trends_tier1",
                "code": "SOCIAL_WB_HOT_NO_USER_KEYWORD_MATCH",
                "message": "命中宏观词后，被 CLI --keywords 进一步过滤为空",
                "hint": "将尝试第二梯队",
            }
        )
        return None

    items: list[dict[str, Any]] = []
    for e in filtered[: max(1, max_items)]:
        hot = e.get("hot") or ""
        url = e.get("url") or ""
        bits: list[str] = []
        if hot:
            bits.append(f"热度 {hot}")
        if url:
            bits.append(url)
        extra = " ".join(bits)
        clean = f"{e['title']} {extra}".strip() if extra else e["title"]
        items.append(_social_item(title=e["title"], clean_text=clean, platform="微博热搜"))
    return items


def _call_tgb_or_hot_rank_em(ak: Any) -> Any:
    """优先淘股吧热榜；当前 akshare 版本可能无此符号，回退东财股吧人气榜。"""
    fn = getattr(ak, "stock_hot_tgb", None)
    if callable(fn):
        return fn(), "akshare:stock_hot_tgb"
    df = ak.stock_hot_rank_em()
    return df, "akshare:stock_hot_rank_em(fallback)"


def _tier2_community_hot_to_items(df: Any, api_label: str) -> list[dict[str, Any]]:
    """将第二梯队 DataFrame 转为 title/clean_text，最多 3 条。"""
    if df is None or getattr(df, "empty", True):
        return []
    import pandas as pd  # noqa: PLC0415

    take = min(3, len(df))
    plat = "淘股吧" if "tgb" in api_label else "东财人气榜"
    items: list[dict[str, Any]] = []
    for _, row in df.head(take).iterrows():
        if "股票名称" in df.columns:
            name = str(row.get("股票名称") or "").strip()
            code = str(row.get("代码") or "").strip()
            rank = row.get("当前排名")
            price = row.get("最新价")
            pct = row.get("涨跌幅")
            title = f"【{plat}】{name}" + (f"（{code}）" if code else "")
            parts = []
            if rank is not None and not (isinstance(rank, float) and pd.isna(rank)):
                try:
                    rnum = float(rank)
                    parts.append(f"排名 {int(rnum)}" if rnum == int(rnum) else f"排名 {rank}")
                except (TypeError, ValueError):
                    parts.append(f"排名 {rank}")
            if price is not None and not (isinstance(price, float) and pd.isna(price)):
                parts.append(f"最新价 {price}")
            if pct is not None and not (isinstance(pct, float) and pd.isna(pct)):
                parts.append(f"涨跌幅 {pct}%")
            clean = "；".join(parts) if parts else title
            items.append(_social_item(title=title, clean_text=clean, platform=plat))
            continue
        # 未知列集（如未来 stock_hot_tgb 字段不同）：退化为首列标题式拼接
        cells = [str(row[c]) for c in df.columns[:6] if row.get(c) is not None and str(row.get(c)).strip()]
        blob = " ".join(cells)
        items.append(_social_item(title=blob[:80], clean_text=blob, platform=plat))
    return items


def fetch_social_trends(
    user_keywords: list[str],
    max_items: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """
    MVP 多级降级社媒热点：① vvhan 微博热搜 → ② AkShare 淘股吧/东财人气榜 → ③ 百度实时热搜 HTML。

    每条为契约字典：title、clean_text（可选 platform 供 markdown）。
    所有异常记入 errors，不向调用方抛掷。

    第三个返回值为 meta：``social_tier_used`` ∈ {1, 2, 3, 0}，``social_api`` 为简要来源标记。
    """
    errors: list[dict[str, Any]] = []
    meta: dict[str, Any] = {"social_tier_used": 0, "social_api": ""}

    try:
        t1 = _tier1_weibo_hot_filtered(user_keywords, max_items, errors)
    except Exception as e:  # noqa: BLE001
        logger.warning("第一梯队微博热搜逻辑异常: %s", repr(e))
        errors.append(
            {
                "source": "social",
                "stage": "social_trends_tier1",
                "code": "SOCIAL_WB_HOT_UNEXPECTED",
                "message": repr(e),
                "hint": "将尝试第二梯队",
            }
        )
        t1 = None

    if t1:
        meta.update({"social_tier_used": 1, "social_api": "vvhan:wbHot+macro_filter"})
        return t1[: max(1, max_items)], errors, meta

    # ---------- 第二梯队 ----------
    items: list[dict[str, Any]] = []
    api_label = "akshare"
    try:
        import akshare as ak  # noqa: PLC0415
    except ImportError as e:
        errors.append(
            {
                "source": "social",
                "stage": "social_trends_tier2",
                "code": "AKSHARE_IMPORT_ERROR",
                "message": str(e),
                "hint": "请安装 akshare 以启用淘股吧/人气榜兜底",
            }
        )
        items = []
    else:
        try:
            df, api_label = _call_tgb_or_hot_rank_em(ak)
            items = _tier2_community_hot_to_items(df, api_label)
        except Exception as e:  # noqa: BLE001
            logger.warning("第二梯队 AkShare 社区热榜失败: %s", repr(e))
            errors.append(
                {
                    "source": "social",
                    "stage": "social_trends_tier2",
                    "code": "SOCIAL_TGB_OR_HOT_RANK_FAILED",
                    "message": repr(e),
                    "hint": "网络或东财/淘股吧接口变更",
                }
            )
            items = []

        if not items:
            errors.append(
                {
                    "source": "social",
                    "stage": "social_trends_tier2",
                    "code": "SOCIAL_TIER2_EMPTY",
                    "message": "AkShare 返回空表或无可用行",
                    "hint": api_label,
                }
            )

    if items:
        errors.append(
            {
                "source": "social",
                "stage": "social_trends",
                "code": "SOCIAL_USED_TIER2_FALLBACK",
                "message": "未使用微博热搜命中或第一梯队不可用，已采用第二梯队结果",
                "hint": api_label,
            }
        )
        meta.update({"social_tier_used": 2, "social_api": api_label})
        return items[: max(1, max_items)], errors, meta

    # ---------- 第三梯队：百度热搜 ----------
    t3 = _tier3_baidu_hot_filtered(user_keywords, max_items, errors)
    if t3:
        errors.append(
            {
                "source": "social",
                "stage": "social_trends",
                "code": "SOCIAL_USED_TIER3_BAIDU",
                "message": "Tier1/2 不可用或为空，已采用百度热搜兜底",
                "hint": BAIDU_BOARD_URL,
            }
        )
        meta.update({"social_tier_used": 3, "social_api": "baidu:board+macro_filter"})
        return t3[: max(1, max_items)], errors, meta

    return [], errors, meta


def fetch_social_section(
    keywords: list[str],
    max_items: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    url = os.environ.get("FINANCE_SOURCE_SOCIAL_API_URL", "").strip()

    if not url:
        items, trend_errs, trend_meta = fetch_social_trends(keywords, max_items)
        errors.extend(trend_errs)
        tier = int(trend_meta.get("social_tier_used") or 0)
        api = str(trend_meta.get("social_api") or "")
        if tier == 1:
            primary = "vvhan:wbHot+macro_filter"
        elif tier == 2:
            primary = api
        elif tier == 3:
            primary = "baidu:board+macro_filter"
        else:
            primary = "social_mvp_empty"
        return (
            {
                "as_of": now_iso(),
                "items": items,
                "source_primary": primary,
                "tier_used": tier,
            },
            errors,
        )

    key = os.environ.get("FINANCE_SOURCE_SOCIAL_API_KEY", "").strip()
    headers = {"User-Agent": "Mozilla/5.0 finance-source-ingest/0.1"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    try:
        req = urlrequest.Request(url, headers=headers)
        with urlrequest.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        body = json.loads(raw)
    except (urlerror.URLError, OSError, ValueError, json.JSONDecodeError) as e:
        errors.append(
            {
                "source": "social",
                "stage": "fetch",
                "code": "SOCIAL_API_FAILED",
                "message": str(e),
                "hint": url,
            }
        )
        # 第三方失败后尝试 MVP 多级降级，避免社媒整段空白
        items_fb, trend_errs, trend_meta = fetch_social_trends(keywords, max_items)
        errors.extend(trend_errs)
        return (
            {
                "as_of": now_iso(),
                "items": items_fb,
                "source_primary": "fallback_after_custom_api",
                "tier_used": int(trend_meta.get("social_tier_used") or 0),
            },
            errors,
        )

    raw_items: list[Any]
    if isinstance(body, list):
        raw_items = body
    elif isinstance(body, dict) and isinstance(body.get("items"), list):
        raw_items = body["items"]
    else:
        errors.append(
            {
                "source": "social",
                "stage": "parse",
                "code": "SOCIAL_API_SHAPE",
                "message": "JSON 需为数组或 {items: []}",
            }
        )
        items_fb, trend_errs, trend_meta = fetch_social_trends(keywords, max_items)
        errors.extend(trend_errs)
        return (
            {
                "as_of": now_iso(),
                "items": items_fb,
                "source_primary": "fallback_after_shape_error",
                "tier_used": int(trend_meta.get("social_tier_used") or 0),
            },
            errors,
        )

    out_items: list[dict[str, Any]] = []
    for it in raw_items:
        if len(out_items) >= max_items:
            break
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or it.get("name") or "").strip()
        link = str(it.get("url") or it.get("link") or "").strip()
        platform = str(it.get("platform") or "").strip() or None
        heat = it.get("heat") or it.get("hot") or it.get("score")
        if keywords:
            blob = f"{title} {link} {platform or ''}".lower()
            if not any(k.lower() in blob for k in keywords):
                continue
        clean_bits = [title]
        if heat is not None and str(heat).strip():
            clean_bits.append(f"热度 {heat}")
        if link:
            clean_bits.append(link)
        clean_text = " ".join(clean_bits[1:]) if len(clean_bits) > 1 else title
        row: dict[str, Any] = {
            "title": title,
            "clean_text": clean_text or title,
            "url": link or None,
            "platform": platform,
            "heat": heat,
        }
        out_items.append(row)

    return (
        {
            "as_of": now_iso(),
            "items": out_items,
            "source_primary": "custom_json_api",
        },
        errors,
    )
