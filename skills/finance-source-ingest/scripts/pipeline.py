"""编排各 fetcher，生成统一 JSON + markdown_summary。"""

from __future__ import annotations

from typing import Any

from _common import compute_invariants, now_iso, truthy_env
from fetchers import market, news_rss, social_api, social_scrape_stub

FOCUS_SECTOR_KEYWORDS = ("科技", "新能源", "港股", "黄金", "银行", "有色")
FOCUS_ALIAS_MAP = {
    "人工智能": "科技",
    "AI": "科技",
    "半导体": "科技",
    "芯片": "科技",
    "算力": "科技",
    "锂电": "新能源",
    "光伏": "新能源",
    "风电": "新能源",
    "储能": "新能源",
    "COMEX 黄金": "黄金",
    "黄金ETF": "黄金",
    "贵金属": "黄金",
    "稀土": "有色",
    "铜": "有色",
    "铝": "有色",
}


def _normalize_focus_label(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    for k, v in FOCUS_ALIAS_MAP.items():
        if k in s:
            return v
    for x in FOCUS_SECTOR_KEYWORDS:
        if x in s:
            return x
    return ""


def _is_focus_related(raw: str) -> bool:
    s = str(raw or "").strip()
    if not s:
        return False
    if _normalize_focus_label(s):
        return True
    return any(k in s for k in [*FOCUS_SECTOR_KEYWORDS, *FOCUS_ALIAS_MAP.keys()])


def _pick_focus_sectors(market_section: dict[str, Any]) -> list[str]:
    rank = (market_section.get("industry_rank") or {}).get("items") or []
    if not isinstance(rank, list):
        return []
    picked: list[str] = []
    for row in rank:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        label = _normalize_focus_label(name)
        if label:
            pct = row.get("pct_change")
            if isinstance(pct, (int, float)):
                picked.append(f"{label}:{name}({pct:+.2f}%)")
            else:
                picked.append(f"{label}:{name}")
    return picked[:6]


def _pick_focus_sectors_from_sentiment(market_section: dict[str, Any]) -> list[str]:
    sentiment = (market_section.get("market_sentiment") or {}).get("hot_keywords") or []
    if not isinstance(sentiment, list):
        return []
    out: list[str] = []
    for kw in sentiment:
        label = _normalize_focus_label(str(kw))
        if label and label not in out:
            out.append(label)
    return out[:6]


def _pick_focus_sectors_from_news(news_section: dict[str, Any]) -> list[str]:
    items = news_section.get("items") or []
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for it in items[:20]:
        if not isinstance(it, dict):
            continue
        blob = f"{it.get('title') or ''} {it.get('clean_text') or ''}"
        label = _normalize_focus_label(blob)
        if label and label not in out:
            out.append(label)
    return out[:6]


def _build_markdown_with_resonance(
    sections: dict[str, Any],
    errors: list[dict[str, Any]],
    fetched_at: str,
) -> str:
    """
    模块 3：共振算法 + 新 Markdown 模板
    - 热词与财联社快讯交叉验证
    - 严格按照 f-string 模板组装，无外部依赖
    """
    m = sections.get("market") or {}
    n = sections.get("news") or {}

    # ---------- 交叉共振算法 ----------
    resonance_text = "今日资金焦点分散，暂未发现与突发快讯强绑定的单一主线。"
    market_sentiment = m.get("market_sentiment") or {}
    hot_keywords = market_sentiment.get("hot_keywords", [])
    hot_keywords_focus = [kw for kw in hot_keywords if _is_focus_related(str(kw))]
    top_hot_stocks = market_sentiment.get("top_hot_stocks", [])

    # 遍历热词，与新闻标题/正文匹配，命中第一个最强共振点即退出
    news_items = n.get("items") or []
    focus_news_items = [
        it for it in news_items if isinstance(it, dict) and _is_focus_related(f"{it.get('title') or ''} {it.get('clean_text') or ''}")
    ]
    resonance_pool = focus_news_items if focus_news_items else news_items
    resonance_keywords = hot_keywords_focus if hot_keywords_focus else hot_keywords
    for keyword in resonance_keywords:
        keyword_lower = str(keyword).lower()
        for news_item in resonance_pool:
            title = str(news_item.get("title") or "").lower()
            clean_text = str(news_item.get("clean_text") or "").lower()
            if keyword_lower in title or keyword_lower in clean_text:
                resonance_text = f"今日资金聚焦【{keyword}】，核心催化事件为：{news_item.get('title')}"
                break
        else:
            continue  # 内层循环未 break，继续下一个 keyword
        break  # 已命中，退出外层循环

    # ---------- 大盘行情数据提取 ----------
    idx = (m.get("a_share_indices") or {}).get("items") or []
    index_parts = []
    for x in idx:
        nm = x.get("name") or ""
        cl = x.get("close")
        pc = x.get("pct_change")
        if cl is not None and isinstance(pc, (int, float)):
            sign = "+" if pc >= 0 else ""
            index_parts.append(f"**{nm}** {cl} ({sign}{pc:.2f}%)")
        elif cl is not None:
            index_parts.append(f"**{nm}** {cl}")
    index_line = " / ".join(index_parts) if index_parts else "暂无行情数据"

    # 北向资金
    nb = m.get("northbound") or {}
    nb_value = nb.get("aggregate_net_buy_yi")
    if nb_value is not None:
        nb_line = f"**北向资金**：{nb_value} 亿元"
    else:
        nb_line = "**北向资金**：暂无数据"

    # 热词展示
    if hot_keywords_focus:
        keywords_display = "、".join(hot_keywords_focus[:6])
    elif hot_keywords:
        keywords_display = "、".join(hot_keywords[:4]) + "（其余已按优先板块收敛）"
    else:
        keywords_display = "暂无数据"

    # 人气股展示
    if top_hot_stocks:
        stocks_display = "、".join([f"{s['name']}({s['code']})" for s in top_hot_stocks])
    else:
        stocks_display = "暂无数据"
    focus_rank = _pick_focus_sectors(m)
    focus_sent = _pick_focus_sectors_from_sentiment(m)
    focus_news = _pick_focus_sectors_from_news(n)
    merged_focus: list[str] = []
    for x in [*focus_rank, *focus_sent, *focus_news]:
        if not x:
            continue
        if x not in merged_focus:
            merged_focus.append(x)
    focus_sector_line = "、".join(merged_focus[:6]) if merged_focus else "暂无命中（优先关注：科技/新能源/港股/黄金/银行/有色）"

    # 市场温度与资金风向
    mt = m.get("market_temperature") or {}
    inflow = mt.get("top_inflow_sectors") or []
    inflow = [
        row for row in inflow if isinstance(row, dict) and _normalize_focus_label(str(row.get("name") or ""))
    ]
    if inflow:
        sector_parts = []
        for row in inflow[:3]:
            name = str(row.get("name") or "").strip()
            val = row.get("main_net_inflow_yi")
            if not name:
                continue
            if isinstance(val, (int, float)):
                sector_parts.append(f"{name}({val:+.2f}亿)")
            else:
                sector_parts.append(name)
        inflow_line = "、".join(sector_parts) if sector_parts else "主力资金流向数据暂缺"
    else:
        inflow_line = "优先板块主力资金流向暂缺"

    lu = mt.get("limit_up_count")
    ld = mt.get("limit_down_count")
    if isinstance(lu, int) and isinstance(ld, int):
        if lu > 80:
            temp_line = f"赚钱效应偏强（涨停 {lu} 家，跌停 {ld} 家），情绪偏亢奋，注意高位分歧。"
        elif ld > 50:
            temp_line = f"风险偏好偏弱（涨停 {lu} 家，跌停 {ld} 家），市场接近冰点，控制节奏。"
        else:
            temp_line = f"情绪中性分化（涨停 {lu} 家，跌停 {ld} 家），关注结构性机会。"
    else:
        temp_line = "赚钱效应数据暂缺（涨跌停统计不可用）。"

    # ---------- 财联社快讯列表 ----------
    news_lines = []
    for it in focus_news_items[:10]:
        hh = news_rss.news_hhmm_for_markdown(str(it.get("published_at") or ""))
        t = (it.get("title") or "").strip()
        if t:
            news_lines.append(f"- [{hh}] {t}")
    if not news_lines and news_items:
        for it in news_items[:3]:
            hh = news_rss.news_hhmm_for_markdown(str(it.get("published_at") or ""))
            t = (it.get("title") or "").strip()
            if t:
                news_lines.append(f"- [{hh}] {t}")

    # ---------- 告警（如有） ----------
    error_lines = []
    if errors:
        error_lines.append(f"**告警 ({len(errors)})**：")
        for e in errors[:5]:
            error_lines.append(f"- [{e.get('code')}] {e.get('message')}")

    # ---------- Python f-string 模板严格组装 ----------
    markdown = f"""## 📊 今日热点快照 ({fetched_at})
---
### 📈 大盘行情
- {index_line}
- {nb_line}
---
### 🌡️ 市场温度与资金风向
- **主力资金风向：** {inflow_line}
- **赚钱效应：** {temp_line}
---
### 🔥 市场情绪与主线焦点
- **收敛过滤：** 优先展示科技/新能源/港股/黄金/银行/有色相关信息
- **资金热搜榜：** {keywords_display}
- **焦点人气股：** {stocks_display}
- **重点板块跟踪：** {focus_sector_line}
- **🎯 主线共振逻辑：** {resonance_text}
---
### 📰 财联社最新快讯
"""
    if news_lines:
        markdown += "\n".join(news_lines)
    else:
        markdown += "暂无财联社快讯数据"

    if error_lines:
        markdown += "\n\n" + "\n".join(error_lines)

    return markdown.rstrip() + "\n"


def build_snapshot(
    sources: list[str],
    keywords: list[str],
    max_items: int,
) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    sections: dict[str, Any] = {}
    sources_ok: list[str] = []

    overseas = truthy_env("FINANCE_SOURCE_OVERSEAS_STUB")
    fetched_at = now_iso()

    if "market" in sources:
        # --- 原有行情区块 ---
        data, errs = market.fetch_market_section(overseas)
        sections["market"] = data
        errors.extend(errs)
        if data.get("a_share_indices") or data.get("northbound") or data.get("industry_rank"):
            sources_ok.append("market")
        mt = data.get("market_temperature") or {}
        if mt.get("top_inflow_sectors") or mt.get("limit_up_count") is not None or mt.get("limit_down_count") is not None:
            sources_ok.append("market.temperature")
        if data.get("overseas_stub"):
            sources_ok.append("market.overseas_stub")

        # --- 新增：市场情绪热点探针 ---
        sentiment_data, sentiment_errs = market.fetch_market_sentiment()
        sections["market"]["market_sentiment"] = sentiment_data
        errors.extend(sentiment_errs)
        if sentiment_data.get("hot_keywords") or sentiment_data.get("top_hot_stocks"):
            sources_ok.append("market.sentiment")

    meta_extra: dict[str, Any] = {}

    if "news" in sources:
        data, errs = news_rss.fetch_news_section(keywords, max_items)
        sections["news"] = data
        errors.extend(errs)
        sources_ok.append("news")
        if data.get("keyword_fallback"):
            meta_extra["news_keyword_fallback"] = True
        if data.get("cls_symbol") is not None:
            meta_extra["news_cls_symbol"] = data["cls_symbol"]

        # ========== 终极兜底：所有外部热词接口失败时，自动从新闻离线提取 ==========
        if "market_sentiment" in sections.get("market", {}):
            sentiment = sections["market"]["market_sentiment"]
            news_items = data.get("items") or []
            if not sentiment.get("hot_keywords") and news_items:
                extracted_kws = market.extract_keywords_from_news(news_items)
                sections["market"]["market_sentiment"]["hot_keywords"] = extracted_kws
                errors.append(
                    {
                        "source": "market",
                        "stage": "market_sentiment_ultimate_fallback",
                        "code": "OFFLINE_KEYWORD_EXTRACT",
                        "message": f"从 {len(news_items)} 条新闻离线提取热词成功",
                        "hint": f"提取到热词: {', '.join(extracted_kws) if extracted_kws else '无'}",
                    }
                )

    if "social" in sources:
        data, errs = social_api.fetch_social_section(keywords, max_items)
        sections["social"] = data
        errors.extend(errs)
        sources_ok.append("social")
        if data.get("tier_used") is not None:
            meta_extra["social_tier_used"] = data.get("tier_used")
        if data.get("source_primary"):
            meta_extra["social_source_primary"] = data.get("source_primary")
        meta_extra["social_scrape_stub"] = social_scrape_stub.fetch_social_scrape_stub()

    # --- 使用新的共振 Markdown 生成器 ---
    md = _build_markdown_with_resonance(sections, errors, fetched_at)
    ok = True

    snapshot: dict[str, Any] = {
        "schema_version": "0.1.0",
        "ok": ok,
        "meta": {
            "fetched_at": fetched_at,
            "timezone": "Asia/Shanghai",
            "sources_requested": sources,
            "sources_ok": sources_ok,
            "keywords": keywords,
            "overseas_stub_requested": overseas,
            **meta_extra,
        },
        "sections": sections,
        "errors": errors,
        "markdown_summary": md,
        "invariants": compute_invariants(),
    }
    return snapshot
