"""按梯队拆分的内部 fetcher（由 pipeline 编排）。

Tier 1: market, news_rss, macro_hot, social_api     — 行情 AkShare+新浪；快讯 RSSHub（FINANCE_RSSHUB_BASE_URL）+ feedparser
Tier 1b: news_sina_live, policy_gov                 — 新浪7x24 全球宏观 + 证监会/央行/CCTV（可选）
Tier 2: deep_news                                   — 可选 FINANCE_RSSHUB_BASE_URL + 华尔街见闻/第一财经/金十/界面 RSS/API
工具层: sentiment                                     — 规则 based 情感分析（零 LLM）
"""
