"""编排各 fetcher，生成统一 JSON + markdown_summary。"""

from __future__ import annotations

from typing import Any

from _common import compute_invariants, now_iso, truthy_env
from fetchers import market, news_rss, social_api, social_scrape_stub


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
    top_hot_stocks = market_sentiment.get("top_hot_stocks", [])

    # 遍历热词，与新闻标题/正文匹配，命中第一个最强共振点即退出
    news_items = n.get("items") or []
    for keyword in hot_keywords:
        keyword_lower = str(keyword).lower()
        for news_item in news_items:
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
    if hot_keywords:
        keywords_display = "、".join(hot_keywords)
    else:
        keywords_display = "暂无数据"

    # 人气股展示
    if top_hot_stocks:
        stocks_display = "、".join([f"{s['name']}({s['code']})" for s in top_hot_stocks])
    else:
        stocks_display = "暂无数据"

    # ---------- 财联社快讯列表 ----------
    news_lines = []
    for it in news_items[:10]:
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
### 🔥 市场情绪与主线焦点
- **资金热搜榜：** {keywords_display}
- **焦点人气股：** {stocks_display}
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
