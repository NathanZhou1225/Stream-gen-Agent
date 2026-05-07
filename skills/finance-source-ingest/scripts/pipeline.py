"""编排各 fetcher，生成统一 JSON + markdown_summary。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import os
import re
import select
import ssl
import subprocess
import sys
from typing import Any
from urllib import error as urllib_error
from urllib import request as urlrequest

from _common import compute_invariants, now_iso, truthy_env
from fetchers import (
    deep_news,
    macro_hot,
    market,
    news_rss,
    news_sina_live,
    policy_gov,
    social_api,
    social_scrape_stub,
)
from fetchers.sector_keywords import SECTOR_KEYWORDS, SECTOR_ORDER, sectors_for_text
from fetchers.sentiment import classify_impact, classify_sentiment, extract_stock_mentions, sentiment_emoji as _s_emoji

logger = logging.getLogger(__name__)


FINANCE_TEXT_HINTS = (
    "A股",
    "港股",
    "美股",
    "股市",
    "股票",
    "股价",
    "股东",
    "板块",
    "债券",
    "国债",
    "汇率",
    "期货",
    "涨停",
    "跌停",
    "涨幅",
    "跌幅",
    "上涨",
    "下跌",
    "央行",
    "美联储",
    "GDP",
    "PMI",
    "通胀",
    "降息",
    "加息",
    "外资",
    "融资",
    "并购",
    "财报",
    "业绩",
    "银行",
    "地产",
    "原油",
    "黄金",
    "白银",
    "贵金属",
    "美元",
    "人民币",
    "产业",
    "出口",
    "进口",
    "关税",
    "国资",
    "财政政策",
    "特别国债",
    "金融",
    "产权市场",
    "交易额",
    "资金",
    "成交",
    "指数",
    "矿产",
    "大宗商品",
)

MAJOR_EVENT_HINTS = (
    "战争",
    "冲突",
    "停火",
    "制裁",
    "地缘",
    "局势",
    "峰会",
    "会议",
    "谈判",
    "协定",
    "条约",
    "协议",
    "央行",
    "美联储",
    "财政部",
    "商务部",
    "发改委",
    "证监会",
    "国资委",
    "自然资源部",
    "国常会",
    "政策",
    "监管",
    "GDP",
    "PMI",
    "通胀",
    "关税",
    "贸易",
    "原油",
    "黄金",
    "出口",
    "进口",
)

_RSSHUB_UPDATE_SCRIPT = "/root/.openclaw/workspace-stream-gen/rsshub/update_rsshub.sh"
_RSSHUB_AUTH_PROMPT = "是否授权 Agent 自动执行底层引擎更新脚本？(y/n): "
_RSSHUB_DIAG_WARN = "⚠️ [Agent 诊断] 检测到宏观资讯抓取为空。这通常意味着目标网站防爬虫规则升级，或 RSSHub 节点路由过期。"


def _news_items_empty(news_section: dict[str, Any]) -> bool:
    items = news_section.get("items")
    return not isinstance(items, list) or len(items) == 0


def _rsshub_self_heal_enabled() -> bool:
    """默认开启；显式设置 FINANCE_RSSHUB_SELF_HEAL=0/false/no 可关闭。"""
    raw = os.environ.get("FINANCE_RSSHUB_SELF_HEAL")
    if raw is None:
        return True
    return raw.strip() in ("1", "true", "TRUE", "yes", "YES")


def _append_rsshub_manual_repair_notice(
    errors: list[dict[str, Any]],
    meta_extra: dict[str, Any],
    *,
    reason: str,
) -> None:
    msg = (
        "RSSHub 新闻抓取为空。请在飞书/微信确认授权后执行更新脚本并重试："
        f"{_RSSHUB_UPDATE_SCRIPT}"
    )
    errors.append(
        {
            "source": "news",
            "stage": "rsshub_self_heal",
            "code": "NEWS_RSSHUB_REPAIR_SUGGESTED",
            "message": msg,
            "hint": reason,
        }
    )
    meta_extra["news_rsshub_manual_action_required"] = True
    meta_extra["news_rsshub_manual_command"] = _RSSHUB_UPDATE_SCRIPT
    meta_extra["news_rsshub_authorization_prompt"] = (
        "⚠️ 报告老板，今天的新闻抓取失败，疑似目标网站防爬虫升级。是否授权我执行底层更新脚本？ [确认执行] | [忽略本次]"
    )


def _prompt_rsshub_self_heal(timeout_sec: int = 18) -> bool:
    logger.warning(_RSSHUB_DIAG_WARN)
    print(_RSSHUB_DIAG_WARN, flush=True)

    if not sys.stdin.isatty():
        logger.warning("[Agent 诊断] 非交互式环境，跳过 RSSHub 自动修复。")
        return False

    print(_RSSHUB_AUTH_PROMPT, end="", flush=True)
    try:
        readable, _, _ = select.select([sys.stdin], [], [], timeout_sec)
        if not readable:
            print("\n[Agent 诊断] 输入超时，跳过自动修复。", flush=True)
            return False
        answer = sys.stdin.readline().strip().lower()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Agent 诊断] 交互授权读取失败，跳过自动修复: %s", exc)
        return False
    return answer in {"y", "yes"}


def _run_rsshub_update_script() -> bool:
    if not os.path.isfile(_RSSHUB_UPDATE_SCRIPT):
        logger.warning("[Agent 诊断] 更新脚本不存在: %s", _RSSHUB_UPDATE_SCRIPT)
        return False

    print("正在拉取开源社区最新修复补丁，请稍候...", flush=True)
    try:
        proc = subprocess.run(
            [_RSSHUB_UPDATE_SCRIPT],
            check=False,
            text=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Agent 诊断] 执行更新脚本失败: %s", exc)
        return False

    if proc.returncode != 0:
        logger.warning("[Agent 诊断] 更新脚本返回非 0: %s", proc.returncode)
        return False
    print("[Agent 诊断] RSSHub 更新完成，正在重试新闻抓取...", flush=True)
    return True

MAJOR_EVENT_ACTOR_HINTS = (
    "中共中央",
    "国务院",
    "习近平",
    "国家",
    "财政部",
    "商务部",
    "发改委",
    "证监会",
    "国资委",
    "央行",
    "美联储",
    "欧盟",
    "联合国",
    "G7",
    "G20",
    "美国",
    "中国",
    "俄罗斯",
    "乌克兰",
    "中东",
    "以色列",
    "伊朗",
    "峰会",
    "协定",
    "条约",
    "战争",
    "冲突",
    "制裁",
)

MAJOR_EVENT_GOV_ACTOR_HINTS = (
    "中共中央",
    "国务院",
    "国家",
    "财政部",
    "商务部",
    "发改委",
    "证监会",
    "国资委",
    "央行",
    "人民银行",
    "美联储",
    "欧盟",
    "联合国",
    "G7",
    "G20",
    "IMF",
    "世界银行",
    "海关",
    "银保监会",
    "金融监管总局",
    "国家统计局",
)

MAJOR_EVENT_EXCLUDE_HINTS = (
    "午评",
    "新闻精选",
    "涨停分析",
    "目标股价",
    "研报",
    "风口研报",
    "机构：",
    "辅导验收",
    "首日挂牌",
    "盘中",
    "大涨",
    "涨停",
    "跌停",
    "业绩报告",
    "营收",
    "净利润",
    "这家公司",
    "公司已",
    "供应商",
    "融资金额",
    "获得新资金",
    "设备",
    "材料",
    "客户",
    "出货",
    "签署战略合作协议",
    "公司（",
    "表示，第一季度",
    "价格均出现",
)

MAJOR_EVENT_STRICT_INCLUDE_HINTS = (
    "央行",
    "美联储",
    "财政",
    "关税",
    "贸易",
    "制裁",
    "战争",
    "冲突",
    "峰会",
    "协定",
    "条约",
    "监管",
    "证监会",
    "发改委",
    "商务部",
    "国务院",
    "国家数据局",
    "国资委",
    "银保监会",
    "金融监管总局",
    "国家统计局",
)

MAJOR_EVENT_THEME_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("黄金/央行储备", ("黄金", "央行", "ETF", "黄金需求")),
    ("货币政策", ("央行", "美联储", "加息", "降息", "利率")),
    ("财政与监管", ("财政", "监管", "证监会", "办法", "政策", "条例")),
    ("贸易与关税", ("关税", "贸易", "出口", "进口", "商务部")),
    ("地缘冲突", ("战争", "冲突", "停火", "制裁", "地缘")),
    ("国家级会议/协定", ("峰会", "协定", "条约", "国常会", "国务院")),
)


def _clip_flash_text(s: str, max_len: int = 160) -> str:
    t = (s or "").strip().replace("\n", " ")
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"


def _item_text(it: dict[str, Any]) -> str:
    return f"{it.get('title') or ''} {it.get('clean_text') or ''} {it.get('summary') or ''} {it.get('detail') or ''}"


def _is_finance_related(it: dict[str, Any]) -> bool:
    if it.get("sector_tags"):
        return True
    txt = _item_text(it)
    return any(k and k in txt for k in FINANCE_TEXT_HINTS)


def _is_major_event(it: dict[str, Any]) -> bool:
    txt = _item_text(it)
    src_name = str(it.get("source_name") or "")
    if any(k and k in txt for k in MAJOR_EVENT_EXCLUDE_HINTS):
        return False
    has_topic = any(k and k in txt for k in MAJOR_EVENT_HINTS)
    has_actor = any(k and k in txt for k in MAJOR_EVENT_ACTOR_HINTS)
    has_regulator_source = any(k in src_name for k in ("证监会", "人民银行", "银保监会", "金融监管总局", "国家统计局"))
    return _is_finance_related(it) and ((has_topic and has_actor) or has_regulator_source)


def _parse_published_at(ts: str) -> datetime | None:
    raw = (ts or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        try:
            dt = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
    return dt.astimezone(timezone(timedelta(hours=8)))


def _major_event_score(it: dict[str, Any], now_dt: datetime) -> float:
    txt = _item_text(it)
    score = 0.0
    title = str(it.get("title") or "")
    body = str(it.get("clean_text") or it.get("title") or "")
    if _is_major_event(it):
        score += 4.0
    if any(k in txt for k in ("盘中宝", "主力资金监控", "概念持续拉升", "触及涨停", "目标价", "午评", "机构观点")):
        score -= 2.5
    if any(k in txt for k in ("央行", "美联储", "财政", "关税", "战争", "冲突", "制裁", "峰会", "协定", "监管", "政策")):
        score += 2.0
    if any(k in txt for k in ("国家数据局", "国务院", "证监会", "发改委", "商务部", "欧盟", "G20", "联合国")):
        score += 1.5
    if len(body) >= 48:
        score += 0.5
    if len(body) >= 120:
        score += 0.8
    if "财联社" in body and "电，" in body and len(body) < 80:
        score -= 0.8
    if "source_kind" in it and it.get("source_kind") == "macro_hot":
        score += 1.2
    if "财联社" in title and "电，" in title and len(title) < 40:
        score -= 0.6
    dt = _parse_published_at(str(it.get("published_at") or ""))
    if dt is not None:
        age_days = max(0.0, (now_dt - dt).total_seconds() / 86400.0)
        if age_days <= 7:
            score += max(0.0, 1.2 - 0.15 * age_days)
        else:
            score -= 1.0
    return score


def _is_wire_brief(it: dict[str, Any]) -> bool:
    title = str(it.get("title") or "").strip()
    body = str(it.get("clean_text") or "").strip()
    txt = f"{title} {body}"
    return ("财联社" in txt and "电，" in txt and len(txt) < 140) or len(body) < 36


def _is_major_event_whitelisted(it: dict[str, Any]) -> bool:
    txt = _item_text(it)
    return any(k in txt for k in MAJOR_EVENT_STRICT_INCLUDE_HINTS)


def _is_corporate_actor_event(it: dict[str, Any]) -> bool:
    txt = _item_text(it)
    if any(k in txt for k in MAJOR_EVENT_GOV_ACTOR_HINTS):
        return False
    corporate_hints = (
        "公司",
        "集团",
        "CFO",
        "CEO",
        "董事会",
        "高管",
        "目标价",
        "财报",
        "业绩",
        "梅赛德斯",
        "特斯拉",
        "星巴克",
    )
    return any(k in txt for k in corporate_hints)


def _major_event_theme_key(it: dict[str, Any]) -> str:
    txt = _item_text(it)
    for theme, hints in MAJOR_EVENT_THEME_HINTS:
        if any(h in txt for h in hints):
            return theme
    title = str(it.get("title") or "").strip()
    return title[:24] if title else "other"


def _format_major_event_digest_line(it: dict[str, Any]) -> str:
    ts_full = str(it.get("published_at") or "").strip()
    date = ts_full[:10] if len(ts_full) >= 10 else ts_full
    title = _clean_display_text(str(it.get("title") or ""))
    body = _clean_display_text(str(it.get("clean_text") or it.get("detail") or title))
    impact = _clip_flash_text(body, max_len=100)
    if impact and title and impact != title:
        return f"- [{date}] **{title}**｜市场影响：{impact}"
    if title:
        return f"- [{date}] **{title}**"
    return f"- [{date}] {impact}" if impact else ""


def _select_weekly_major_lines(
    all_news_items: list[dict[str, Any]],
    macro_items: list[dict[str, Any]],
    *,
    fetched_at: str,
    limit: int = 5,
) -> list[str]:
    now_dt = _parse_published_at(fetched_at) or datetime.now(timezone(timedelta(hours=8)))
    week_start = now_dt - timedelta(days=7)
    scored: list[tuple[float, datetime, dict[str, Any]]] = []
    for it in all_news_items:
        if not isinstance(it, dict):
            continue
        if not _is_finance_related(it):
            continue
        dt = _parse_published_at(str(it.get("published_at") or ""))
        if dt is None or dt < week_start:
            continue
        s = _major_event_score(it, now_dt)
        if s < 2.5:
            continue
        scored.append((s, dt, it))

    for x in macro_items:
        if not isinstance(x, dict):
            continue
        detail = str(x.get("detail") or "").strip()
        title = str(x.get("title") or "").strip()
        if not title or not detail:
            continue
        it = {
            "title": title,
            "clean_text": detail,
            "detail": detail,
            "published_at": fetched_at,
            "source_kind": "macro_hot",
        }
        s = _major_event_score(it, now_dt)
        if s < 3.2:
            continue
        scored.append((s, now_dt, it))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

    lines: list[str] = []
    seen_title: set[str] = set()
    seen_theme: set[str] = set()
    for _, _, it in scored:
        if not _is_major_event_whitelisted(it):
            continue
        if _is_corporate_actor_event(it):
            continue
        if _is_wire_brief(it) and not any(
            k in _item_text(it) for k in ("央行", "证监会", "财政", "关税", "战争", "冲突", "制裁", "峰会", "协定")
        ):
            continue
        theme = _major_event_theme_key(it)
        if theme in seen_theme:
            continue
        title = str(it.get("title") or "").strip()
        norm = title[:36]
        if norm and norm in seen_title:
            continue
        if norm:
            seen_title.add(norm)
        seen_theme.add(theme)
        line = _format_major_event_digest_line(it)
        if line:
            lines.append(line)
        if len(lines) >= limit:
            break
    return lines


def _format_flash_line(it: dict[str, Any], *, max_len: int = 180) -> str:
    ts_full = str(it.get("published_at") or "").strip()
    hh = news_rss.news_hhmm_for_markdown(str(it.get("published_at") or ""))
    tpart = f"[{ts_full[:19]}]" if len(ts_full) >= 16 else f"[{hh}]"
    body = _clip_flash_text(str(it.get("clean_text") or it.get("title") or ""), max_len=max_len)
    return f"- {tpart} {body}" if body else ""


def _market_sector_fallback(sec: str, market_section: dict[str, Any]) -> str:
    keyword_map = {
        "科技": ("科技", "AI", "算力", "芯片", "半导体", "软件", "互联网"),
        "新能源": ("新能源", "电池", "锂", "储能", "光伏", "风电", "能源金属"),
        "港股": ("港股", "恒生", "南向", "恒生科技"),
        "黄金": ("黄金", "贵金属", "金价"),
        "有色": ("有色", "小金属", "能源金属", "铜", "铝", "锌", "稀土", "钢铁"),
        "银行": ("银行", "信贷", "息差", "降息", "降准", "LPR"),
    }
    keys = keyword_map.get(sec, (sec,))
    hits: list[str] = []
    ir = (market_section.get("industry_rank") or {}).get("items") or []
    if isinstance(ir, list):
        for row in ir[:12]:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            pct = row.get("pct_change")
            if not name or not any(k and k in name for k in keys):
                continue
            if isinstance(pct, (int, float)):
                hits.append(f"{name}({pct:+.2f}%)")
            else:
                hits.append(name)
    sent = market_section.get("market_sentiment") or {}
    kws = [str(x) for x in (sent.get("hot_keywords") or []) if str(x).strip()]
    kw_hits = [x for x in kws if any(k and k in x for k in keys)]
    if kw_hits:
        hits.append("热词：" + "、".join(kw_hits[:4]))
    if sec == "港股":
        hk_items = (market_section.get("hong_kong_indices") or {}).get("items") or []
        if isinstance(hk_items, list):
            hp: list[str] = []
            for row in hk_items[:3]:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("name") or "").strip()
                close = row.get("close")
                pct = row.get("pct_change")
                if name and isinstance(close, (int, float)) and isinstance(pct, (int, float)):
                    hp.append(f"{name} {close}({pct:+.2f}%)")
                elif name and isinstance(pct, (int, float)):
                    hp.append(f"{name}({pct:+.2f}%)")
            if hp:
                hits.append("港股指数：" + "、".join(hp))
    if hits:
        return f"- （RSSHub 快讯本轮未命中；行情侧补充）{ '；'.join(hits[:4]) }"
    return f"- （本轮 RSSHub 快讯与行情池暂未命中{sec}可用信息）"


def _market_sector_fill_lines(sec: str, market_section: dict[str, Any], *, min_count: int = 3) -> list[str]:
    """当板块新闻不足时，用行情字段补齐条数。"""
    lines: list[str] = []
    first = _market_sector_fallback(sec, market_section)
    if first:
        lines.append(first)

    keys = SECTOR_KEYWORDS.get(sec, ())
    sent = market_section.get("market_sentiment") or {}
    kws = [str(x) for x in (sent.get("hot_keywords") or []) if str(x).strip()]
    kw_hits = [x for x in kws if any(k and k in x for k in keys)]
    if kw_hits:
        lines.append("- （行情侧补充）市场热词：" + "、".join(kw_hits[:4]))

    inflow = (market_section.get("market_temperature") or {}).get("top_inflow_sectors") or []
    inflow_hits: list[str] = []
    if isinstance(inflow, list):
        for row in inflow:
            if not isinstance(row, dict):
                continue
            nm = str(row.get("name") or "").strip()
            v = row.get("main_net_inflow_yi")
            if not nm or not any(k and k in nm for k in keys):
                continue
            if isinstance(v, (int, float)):
                inflow_hits.append(f"{nm}({v:+.2f}亿)")
            else:
                inflow_hits.append(nm)
    if inflow_hits:
        lines.append("- （行情侧补充）主力净流入：" + "、".join(inflow_hits[:3]))

    if sec == "有色":
        ir = (market_section.get("industry_rank") or {}).get("items") or []
        nonferrous_hits: list[str] = []
        if isinstance(ir, list):
            for row in ir[:15]:
                if not isinstance(row, dict):
                    continue
                nm = str(row.get("name") or "").strip()
                pct = row.get("pct_change")
                if not nm:
                    continue
                if any(k in nm for k in ("有色", "能源金属", "稀土", "工业金属", "铜", "铝", "锌", "镍")):
                    if isinstance(pct, (int, float)):
                        nonferrous_hits.append(f"{nm}({pct:+.2f}%)")
                    else:
                        nonferrous_hits.append(nm)
        if nonferrous_hits:
            lines.append("- （有色专属补充）行业涨跌：" + "、".join(nonferrous_hits[:4]))

    if sec == "港股":
        hk_items = (market_section.get("hong_kong_indices") or {}).get("items") or []
        hp: list[str] = []
        if isinstance(hk_items, list):
            for row in hk_items[:3]:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("name") or "").strip()
                close = row.get("close")
                pct = row.get("pct_change")
                if name and isinstance(close, (int, float)) and isinstance(pct, (int, float)):
                    hp.append(f"{name} {close}({pct:+.2f}%)")
                elif name and isinstance(pct, (int, float)):
                    hp.append(f"{name}({pct:+.2f}%)")
        if hp:
            hk_tail = "、".join(hp)
            if not any(hk_tail in x for x in lines):
                lines.append("- （行情侧补充）港股指数：" + hk_tail)

    deduped: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if "港股指数：" in line:
            k = "hkidx:" + _title_dedup_key(line.split("港股指数：", 1)[-1])
        else:
            k = _title_dedup_key(line)
        if k and k not in seen:
            seen.add(k)
            deduped.append(line)
        if len(deduped) >= min_count:
            break
    while len(deduped) < min_count:
        deduped.append("- （行情侧补充）该板块当日新闻源稀缺，已展示盘面核心指标。")
    return deduped


# ─── 今日热点重要度评分常量 ───────────────────────────────────────────────

_HOTSPOT_REG_SOURCES: tuple[str, ...] = (
    "证监会", "人民银行", "发改委", "财政部", "国资委",
    "银保监会", "金融监管总局", "国家统计局", "商务部",
)

_HOTSPOT_PRIORITY_KEYWORDS: tuple[str, ...] = (
    "央行", "美联储", "财政", "关税", "降息", "加息",
    "制裁", "战争", "冲突", "贸易战", "出口管制", "降准",
)


def _title_dedup_key(s: str) -> str:
    """标题去重 key：去空白、小写、取前 30 字。"""
    cleaned = re.sub(r"<[^>]+>", " ", (s or ""))
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "", cleaned.lower())
    return re.sub(r"\s+", "", cleaned)[:30]


def _router_event_dedup_key(it: dict[str, Any]) -> str:
    """路由结果跨板块去重：弱化「财联社5月7日电」等与正文前缀差异，对齐同一快讯多源抄送。"""
    title = _clean_display_text(str(it.get("title") or ""))
    title = re.sub(r"^财联社\d{1,2}月\d{1,2}日电[，,、：:\s]*", "", title)
    title = re.sub(r"^新浪财经\d{1,2}月\d{1,2}日讯[，,、：:\s]*", "", title)
    title = re.sub(r"^【[^】]{0,40}】\s*", "", title)
    k = _title_dedup_key(title)
    if len(k) >= 10:
        return k
    body = _clean_display_text(str(it.get("clean_text") or it.get("summary") or ""))[:120]
    return _title_dedup_key(body)


def _dedupe_router_items_across_sectors(
    items_by_sec: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """同一事件仅在六大板块中保留一条：正文更长优先，并列时保留板块顺序更靠前的一条。"""
    out: dict[str, list[dict[str, Any]]] = {s: [] for s in _ROUTER_SECTORS}
    dedup_queue: list[tuple[int, int, dict[str, Any], str]] = []
    for si, sec in enumerate(_ROUTER_SECTORS):
        for it in items_by_sec.get(sec) or []:
            if not isinstance(it, dict):
                continue
            dk = _router_event_dedup_key(it)
            if not dk or len(dk) < 8:
                out[sec].append(it)
                continue
            bl = len(str(it.get("clean_text") or it.get("summary") or ""))
            dedup_queue.append((bl, si, it, dk))
    best: dict[str, tuple[int, int, dict[str, Any]]] = {}
    for bl, si, it, dk in dedup_queue:
        prev = best.get(dk)
        if prev is None or (bl, -si) > (prev[0], -prev[1]):
            best[dk] = (bl, si, it)
    for _dk, (_bl, si, it) in best.items():
        out[_ROUTER_SECTORS[si]].append(it)
    return out


def _clean_display_text(s: str) -> str:
    """显示层文本净化：去 HTML / 折叠空白。"""
    txt = re.sub(r"<[^>]+>", " ", (s or ""))
    # 兜底清理被截断的 HTML 残片（如 "<br/..."、"<span class..."）
    txt = re.sub(r"<[^\s]{0,60}\.\.\.", " ", txt)
    txt = re.sub(r"<\s*/?\s*[a-zA-Z][^>\n\r]{0,120}", " ", txt)
    txt = txt.replace("\\n", " ").replace("\n", " ").replace("\r", " ").replace("\xa0", " ")
    txt = txt.replace("<", " ").replace(">", " ")
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def _sector_strong_match(
    sec: str,
    title: str,
    body: str,
    item_tags: list[str] | tuple[str, ...] | None = None,
    vertical_target_sector: str | None = None,
) -> bool:
    """板块强相关判定，避免弱相关误命中。"""
    # 第一段：标签豁免权（垂直路由或上游已打板块标签时直接放行）
    if vertical_target_sector and str(vertical_target_sector).strip() == sec:
        return True
    if item_tags and sec in {str(x).strip() for x in item_tags if str(x).strip()}:
        return True

    # 第二段：关键词强匹配（用于宽池条目）
    blob = f"{title} {body}"
    if sec == "黄金":
        return any(
            k in blob
            for k in (
                "金价",
                "现货黄金",
                "COMEX",
                "贵金属",
                "纽约期金",
                "沪金",
                "黄金ETF",
                "金银比",
                "伦敦金",
                "非农",
                "美联储",
                "降息",
                "鲍威尔",
            )
        )
    if sec == "有色":
        return any(
            k in blob
            for k in (
                "有色",
                "能源金属",
                "稀土",
                "工业金属",
                "铜",
                "铝",
                "锌",
                "镍",
                "钴",
                "锂矿",
                "钨",
                "伦铜",
                "沪铜",
                "LME",
                "氧化铝",
                "电解铝",
                "铅",
                "锡",
                "镍价",
                "碳酸锂",
                "小金属",
                "特种金属",
            )
        )
    return True


_ROUTER_SECTORS: tuple[str, ...] = ("科技", "新能源", "港股", "黄金", "有色", "银行")
_ROUTER_SYSTEM_PROMPT = (
    "你是一名专业的金融分析师。请阅读以下今日新闻菜单，为 [科技, 新能源, 港股, 黄金, 有色, 银行] "
    "这六大板块分别挑选 1-3 条最有价值的资讯。\n"
    "挑选原则：\n"
    "(1) 优先挑选具有基本面/资金面深度逻辑的重大新闻。\n"
    "(2) 如果某板块今日无重大事件，请退而求其次，挑选最相关的行业动态或盘面异动，只要与该板块【强相关】即可保留。\n"
    "(3) 仅在菜单中完全没有该板块任何相关信息时，该板块才返回 []。\n"
    "(4) 同一事件的不同快讯请勿重复挑选。\n"
    "输出要求：只返回一个严格 JSON 对象，且必须同时包含上述六个中文键；值为菜单项整数 ID 的列表（每板块 0～3 个 ID），"
    "同一 ID 尽量不要重复出现在多个板块。示例："
    '{"科技": [1], "新能源": [2, 5], "港股": [0], "黄金": [], "有色": [3], "银行": []}。'
    "严禁输出任何解释、思考过程或其它字符。"
)


def _router_enabled() -> bool:
    raw = os.environ.get("FINANCE_LLM_ROUTER_ENABLED", "").strip()
    if not raw:
        return True
    return raw in ("1", "true", "TRUE", "yes", "YES")


def _router_menu_max_items() -> int:
    raw = os.environ.get("FINANCE_LLM_ROUTER_MENU_MAX_ITEMS", "").strip()
    try:
        v = int(raw) if raw else 18
    except ValueError:
        v = 18
    return max(12, min(40, v))


def _router_timeout_sec() -> int:
    raw = os.environ.get("FINANCE_LLM_ROUTER_TIMEOUT_SEC", "").strip()
    try:
        v = int(raw) if raw else 35
    except ValueError:
        v = 20
    return max(5, min(60, v))


def _router_load_config() -> tuple[str, str, str]:
    base = os.environ.get("FINANCE_LLM_ROUTER_BASE_URL", "").strip()
    key = os.environ.get("FINANCE_LLM_ROUTER_API_KEY", "").strip()
    model = os.environ.get("FINANCE_LLM_ROUTER_MODEL", "").strip()
    if base and key and model:
        return base.rstrip("/"), key, model

    ark_base = os.environ.get("OPENCLAW_ARK_BASE_URL", "").strip()
    ark_key = os.environ.get("OPENCLAW_ARK_API_KEY", "").strip()
    ark_model = os.environ.get("OPENCLAW_ARK_MODEL", "ark-code-latest").strip()
    if ark_base and ark_key:
        return ark_base.rstrip("/"), ark_key, (model or ark_model)

    cfg_path = os.environ.get("OPENCLAW_CONFIG", "/root/.openclaw/openclaw.json")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            conf = json.load(f)
    except Exception:  # noqa: BLE001
        conf = {}
    prov = (((conf.get("models") or {}).get("providers") or {}).get("ark") or {})
    b2 = str(prov.get("baseUrl") or "").strip()
    k2 = str(prov.get("apiKey") or "").strip()
    models = prov.get("models") or []
    m2 = str(models[0].get("id") if models and isinstance(models[0], dict) else "") or ark_model
    if b2 and k2:
        return b2.rstrip("/"), k2, (model or m2)
    raise RuntimeError("LLM router 未配置可用网关（FINANCE_LLM_ROUTER_* 或 OPENCLAW_ARK_* / openclaw.json）")


def _router_parse_json(raw: str) -> dict[str, list[int]]:
    s = (raw or "").strip()
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", s)
        if not m:
            raise ValueError("router 输出无法解析为 JSON object") from None
        obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("router 输出根节点必须为 object")
    out: dict[str, list[int]] = {sec: [] for sec in _ROUTER_SECTORS}
    for sec in _ROUTER_SECTORS:
        vals = obj.get(sec) or []
        if not isinstance(vals, list):
            continue
        arr: list[int] = []
        for x in vals:
            if isinstance(x, int):
                arr.append(x)
            elif isinstance(x, str) and x.strip().isdigit():
                arr.append(int(x.strip()))
        out[sec] = arr
    return out


def _router_build_candidates(
    by_sec: dict[str, list[dict[str, Any]]],
    gm_items: list[dict[str, Any]],
    deep_items: list[dict[str, Any]],
    other_flash_items: list[dict[str, Any]],
    macro_items_raw: list[dict[str, Any]],
    *,
    max_items: int = 40,
) -> list[dict[str, Any]]:
    pool: list[dict[str, Any]] = []
    seen: set[str] = set()

    def push(it: dict[str, Any], default_source: str = "") -> None:
        title = _clean_display_text(str(it.get("title") or "")).strip()
        body = _clean_display_text(str(it.get("clean_text") or it.get("summary") or "")).strip()
        if not title and not body:
            return
        key = _title_dedup_key(title or body[:80])
        if not key or key in seen:
            return
        seen.add(key)
        pool.append(
            {
                "title": title or body[:120],
                "summary": body[:120],
                "clean_text": str(it.get("clean_text") or it.get("summary") or title),
                "source_name": str(it.get("source_name") or default_source or _item_source_label(it)),
                "published_at": str(it.get("published_at") or ""),
                "raw_item": dict(it),
            }
        )

    for sec in _ROUTER_SECTORS:
        for it in (by_sec.get(sec) or [])[:6]:
            if isinstance(it, dict):
                push(it)
    for bucket in (gm_items, deep_items, other_flash_items):
        for it in bucket[:30]:
            if isinstance(it, dict):
                push(it)
    for it in macro_items_raw[:12]:
        if not isinstance(it, dict):
            continue
        synth = {
            "title": str(it.get("title") or ""),
            "clean_text": str(it.get("detail") or it.get("title") or ""),
            "published_at": str(it.get("published_at") or ""),
            "source_name": "百度热榜",
        }
        push(synth, default_source="百度热榜")
    pool.sort(key=lambda x: _parse_published_at(x.get("published_at") or "") or datetime.min.replace(tzinfo=timezone(timedelta(hours=8))), reverse=True)
    return pool[:max_items]


def _router_build_menu(candidates: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for idx, it in enumerate(candidates):
        src = str(it.get("source_name") or "未知来源").strip()
        title = _clean_display_text(str(it.get("title") or "")).strip()
        summ = _clean_display_text(str(it.get("summary") or "")).strip()[:20]
        lines.append(f"[ID: {idx}] {src} - {title} - {summ}")
    return "\n".join(lines)


def _router_call_llm(menu_text: str, *, timeout_sec: int = 10) -> dict[str, list[int]]:
    base, key, model = _router_load_config()
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _ROUTER_SYSTEM_PROMPT},
            {"role": "user", "content": menu_text},
        ],
        "temperature": 0.1,
        "max_tokens": 260,
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(
        f"{base}/chat/completions",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            t = max(1, timeout_sec + (attempt - 1) * 8)
            with urlrequest.urlopen(req, timeout=t, context=ssl.create_default_context()) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except urllib_error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"router HTTP {e.code}: {detail[:500]}") from e
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if attempt >= 2:
                raise RuntimeError(f"router 请求失败: {e!s}") from e
    else:
        raise RuntimeError(f"router 请求失败: {last_exc!s}")
    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError("router 响应无 choices")
    content = str(((choices[0] or {}).get("message") or {}).get("content") or "")
    if not content.strip():
        raise RuntimeError("router content 为空")
    return _router_parse_json(content)


def _legacy_sector_enriched(
    by_sec: dict[str, list[dict[str, Any]]],
    gm_items: list[dict[str, Any]],
    deep_items: list[dict[str, Any]],
    other_flash_items: list[dict[str, Any]],
    macro_items_raw: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    return _enrich_sectors_from_all_sources(
        by_sec,
        gm_items,
        deep_items,
        list(other_flash_items),
        list(macro_items_raw),
        max_per_sector=5,
    )


def _build_sector_items_with_router(
    by_sec: dict[str, list[dict[str, Any]]],
    gm_items: list[dict[str, Any]],
    deep_items: list[dict[str, Any]],
    other_flash_items: list[dict[str, Any]],
    macro_items_raw: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    legacy = _legacy_sector_enriched(by_sec, gm_items, deep_items, other_flash_items, macro_items_raw)
    if not _router_enabled():
        return legacy, {"status": "disabled_by_env"}
    candidates = _router_build_candidates(
        by_sec,
        gm_items,
        deep_items,
        other_flash_items,
        macro_items_raw,
        max_items=_router_menu_max_items(),
    )
    if not candidates:
        return legacy, {"status": "no_candidates"}
    menu_text = _router_build_menu(candidates)
    try:
        routed = _router_call_llm(menu_text, timeout_sec=_router_timeout_sec())
        items_by_sec: dict[str, list[dict[str, Any]]] = {sec: [] for sec in _ROUTER_SECTORS}
        global_idx: set[int] = set()
        for sec in _ROUTER_SECTORS:
            seen_idx: set[int] = set()
            for idx in routed.get(sec) or []:
                if not isinstance(idx, int):
                    continue
                if idx < 0 or idx >= len(candidates) or idx in seen_idx or idx in global_idx:
                    continue
                seen_idx.add(idx)
                global_idx.add(idx)
                base_item = dict(candidates[idx].get("raw_item") or {})
                if not base_item:
                    continue
                if not base_item.get("clean_text"):
                    base_item["clean_text"] = candidates[idx].get("clean_text") or candidates[idx].get("summary") or base_item.get("title") or ""
                tags = base_item.get("sector_tags") or []
                tags_clean = [str(x).strip() for x in tags if str(x).strip()]
                if sec not in tags_clean:
                    tags_clean.insert(0, sec)
                base_item["sector_tags"] = tags_clean
                base_item["vertical_target_sector"] = sec
                base_item["sector_line_source"] = "llm_router"
                items_by_sec[sec].append(base_item)
        items_by_sec = _dedupe_router_items_across_sectors(items_by_sec)
        return items_by_sec, {
            "status": "ok",
            "selected_count": sum(len(v) for v in items_by_sec.values()),
            "menu_count": len(candidates),
            "menu_preview": menu_text[:2000],
        }
    except Exception as exc:  # noqa: BLE001
        errors.append(
            {
                "source": "llm_router",
                "stage": "dispatch",
                "code": "LLM_ROUTER_FAILED",
                "message": str(exc)[:500],
            }
        )
        return legacy, {
            "status": "fallback_legacy",
            "reason": str(exc)[:300],
            "menu_count": len(candidates),
        }


def _sector_source_rank(item: dict[str, Any], sec: str) -> int:
    """板块内来源优先级（值越大越优先）。"""
    src = str(item.get("sector_line_source") or "")
    if sec == "有色":
        if src == "deep_news":
            return 5
        if src == "global_macro":
            return 4
        if src == "other_flash":
            return 3
        if src in ("tagged", "tagged_catchup", "recent_keyword"):
            return 2
        return 1
    if src == "deep_news":
        return 4
    if src == "global_macro":
        return 3
    return 2


def _item_source_label(it: dict[str, Any]) -> str:
    """提取展示用来源标注文字。"""
    src_name = str(it.get("source_name") or "").strip()
    if src_name:
        return src_name
    src_hint = str(it.get("source_hint") or "").strip()
    if src_hint == "cls_akshare":
        return "财联社"
    if it.get("cross_source_hit"):
        return "财联社·RSSHub"
    return "RSSHub快讯"


def _hotspot_importance_score(it: dict[str, Any]) -> float:
    """今日热点重要度评分（越高越重要）。"""
    txt = _item_text(it)
    src_name = str(it.get("source_name") or "")
    score = 0.0
    # 来源为监管/央行机构
    if any(k in src_name for k in _HOTSPOT_REG_SOURCES):
        score += 3.0
    # 内容含监管/央行关键词
    if any(k in txt for k in _HOTSPOT_REG_SOURCES):
        score += 2.0
    # 内容含市场触发词
    if any(k in txt for k in _HOTSPOT_PRIORITY_KEYWORDS):
        score += 1.5
    if _is_finance_related(it):
        score += 0.5
    # 时效性加分
    now_dt = datetime.now(timezone(timedelta(hours=8)))
    dt = _parse_published_at(str(it.get("published_at") or ""))
    if dt is not None:
        age_h = max(0.0, (now_dt - dt).total_seconds() / 3600.0)
        if age_h <= 4:
            score += 1.0
        elif age_h <= 12:
            score += 0.5
    return score


def _enrich_sectors_from_all_sources(
    base_sectors: dict[str, list[dict[str, Any]]],
    global_macro_items: list[dict[str, Any]],
    deep_news_items: list[dict[str, Any]],
    flash_items: list[dict[str, Any]],
    macro_hot_items: list[dict[str, Any]],
    *,
    max_per_sector: int = 5,
) -> dict[str, list[dict[str, Any]]]:
    """将 global_macro 和 deep_news 中的板块相关条目合并进六大板块桶（不超 max_per_sector）。"""
    result: dict[str, list[dict[str, Any]]] = {}
    for sec in SECTOR_ORDER:
        result[sec] = list(base_sectors.get(sec) or [])

    existing_keys: dict[str, set[str]] = {
        sec: {_title_dedup_key(str(x.get("title") or "")) for x in items}
        for sec, items in result.items()
    }

    def try_add(sec: str, it: dict[str, Any]) -> None:
        if len(result[sec]) >= max_per_sector:
            return
        k = _title_dedup_key(str(it.get("title") or ""))
        if not k or k in existing_keys[sec]:
            return
        existing_keys[sec].add(k)
        result[sec].append(it)

    # 补充 global_macro 条目
    for it in global_macro_items:
        if not isinstance(it, dict):
            continue
        body = str(it.get("clean_text") or it.get("title") or "")
        txt = f"{it.get('title') or ''} {body}"
        sec_tags = sectors_for_text(txt)
        if not sec_tags:
            continue
        neo = dict(it)
        if "sentiment_hint" not in neo:
            s = classify_sentiment(txt)
            neo["sentiment_hint"] = s
            neo["sentiment_emoji"] = _s_emoji(s)
            neo["impact_level"] = classify_impact(txt)
        if not neo.get("clean_text"):
            neo["clean_text"] = neo.get("title") or ""
        neo["sector_tags"] = sec_tags
        neo["sector_line_source"] = "global_macro"
        for sec in sec_tags:
            if sec in result:
                try_add(sec, neo)

    # 补充 deep_news 条目
    for it in deep_news_items:
        if not isinstance(it, dict):
            continue
        sec_tags = it.get("sector_tags") or []
        if not sec_tags:
            body = str(it.get("summary") or it.get("clean_text") or "")
            sec_tags = sectors_for_text(f"{it.get('title') or ''} {body}")
        if not sec_tags:
            continue
        neo = dict(it)
        neo["sector_line_source"] = "deep_news"
        if not neo.get("clean_text") and neo.get("summary"):
            neo["clean_text"] = neo["summary"]
        for sec in sec_tags:
            if sec in result:
                try_add(sec, neo)

    # 补充其他金融快讯
    for it in flash_items:
        if not isinstance(it, dict):
            continue
        txt = _item_text(it)
        sec_tags = sectors_for_text(txt)
        if not sec_tags:
            continue
        neo = dict(it)
        neo["sector_line_source"] = "other_flash"
        for sec in sec_tags:
            if sec in result:
                try_add(sec, neo)

    # 补充 macro_hot（百度）条目
    for it in macro_hot_items:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        detail = str(it.get("detail") or "").strip()
        txt = f"{title} {detail}"
        sec_tags = sectors_for_text(txt)
        if not sec_tags:
            continue
        neo = {
            "title": title or detail[:60],
            "clean_text": detail or title,
            "published_at": it.get("published_at") or "",
            "source_name": "百度热榜",
            "sector_line_source": "macro_hot",
            "sentiment_hint": classify_sentiment(txt),
            "sentiment_emoji": _s_emoji(classify_sentiment(txt)),
        }
        for sec in sec_tags:
            if sec in result:
                try_add(sec, neo)

    return result


def _build_hotspot_top5(
    global_macro_items: list[dict[str, Any]],
    deep_news_items: list[dict[str, Any]],
    flash_items: list[dict[str, Any]],
    macro_hot_items: list[dict[str, Any]],
    enriched_sectors: dict[str, list[dict[str, Any]]],
    major_event_items: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """从全部信源池化，筛选 5 条最重要的今日热点（排除大事件和六大板块已有内容）。"""
    # 构建板块桶中已有条目的去重 key
    in_sector_keys: set[str] = set()
    for items in enriched_sectors.values():
        for it in items:
            in_sector_keys.add(_title_dedup_key(str(it.get("title") or "")))

    pool: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    major_keys = {
        _title_dedup_key(str(x.get("title") or ""))
        for x in (major_event_items or [])
        if isinstance(x, dict)
    }

    def add_if_new(it: dict[str, Any]) -> None:
        title = str(it.get("title") or "").strip()
        # 过滤过短的导航/栏目标题（如「金融知识」「要闻」等）
        if len(title) <= 6:
            return
        k = _title_dedup_key(title)
        if not k:
            return
        if k in in_sector_keys:
            return
        if k in major_keys:
            return
        # 已属于「大事件」级别的内容已在大事件板块展示，此处跳过
        if _is_major_event(it) and _is_major_event_whitelisted(it):
            return
        if k in seen_keys:
            return
        seen_keys.add(k)
        pool.append(it)

    # 按来源重要性顺序入池
    for it in global_macro_items:
        if isinstance(it, dict) and _is_finance_related(it):
            add_if_new(it)
    for it in deep_news_items:
        if isinstance(it, dict):
            add_if_new(it)
    for it in flash_items:
        if isinstance(it, dict) and _is_finance_related(it):
            add_if_new(it)
    for it in macro_hot_items:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        detail = str(it.get("detail") or "").strip()
        if not title:
            continue
        synthesized = {
            "title": title,
            "clean_text": detail,
            "published_at": it.get("published_at") or "",
            "source_name": "百度热榜",
        }
        if _is_finance_related(synthesized):
            add_if_new(synthesized)

    pool.sort(key=_hotspot_importance_score, reverse=True)
    return pool[:5]


def _build_live_stream_markdown(
    sections: dict[str, Any],
    errors: list[dict[str, Any]],
    fetched_at: str,
) -> str:
    """全量快照 Markdown：大盘与情绪 → 六大板块（含 clean_text 摘要）→ 宏观与舆情（百度+社媒）。"""
    m = sections.get("market") or {}
    n = sections.get("news") or {}
    macro_sec = sections.get("macro_hot") or {}
    soc = sections.get("social") or {}

    idx = (m.get("a_share_indices") or {}).get("items") or []
    index_parts: list[str] = []
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

    src_bits: list[str] = [f"**三大指数**：{index_line}"]
    a_src = (m.get("a_share_indices") or {}).get("source") or ""

    nb = m.get("northbound") or {}
    nb_val = nb.get("aggregate_net_buy_yi")
    if nb_val is not None:
        if isinstance(nb_val, (int, float)) and abs(float(nb_val)) < 0.0001:
            src_bits.append("**北向资金**（接口返回 0，疑似未更新或口径异常；请以交易所/东财等渠道核验）")
        else:
            src_bits.append(f"**北向资金**（尽力探测）：{nb_val} 亿元")
    else:
        src_bits.append("**北向资金**：今日数据暂缺（接口失败或未返回）")

    if "sina" in str(a_src).lower():
        src_bits.append("📡 指数源：新浪财经 hq.sinajs.cn")

    ir = (m.get("industry_rank") or {}).get("items") or []
    if isinstance(ir, list) and ir:
        parts = []
        for row in ir[:5]:
            if not isinstance(row, dict):
                continue
            nm = str(row.get("name") or "").strip()
            pc = row.get("pct_change")
            if nm and isinstance(pc, (int, float)):
                parts.append(f"{nm} ({pc:+.2f}%)")
            elif nm:
                parts.append(nm)
        if parts:
            src_bits.append("**行业强弱 Top**：" + "、".join(parts))

    hk = (m.get("hong_kong_indices") or {}).get("items") or []
    if isinstance(hk, list) and hk:
        hp = []
        for row in hk[:3]:
            if not isinstance(row, dict):
                continue
            nm = str(row.get("name") or "").strip()
            cl = row.get("close")
            pc = row.get("pct_change")
            if nm and isinstance(cl, (int, float)) and isinstance(pc, (int, float)):
                hp.append(f"{nm} {cl}({pc:+.2f}%)")
            elif nm and isinstance(pc, (int, float)):
                hp.append(f"{nm}({pc:+.2f}%)")
        if hp:
            src_bits.append("**港股指数**（新浪）： " + "、".join(hp))

    mt = m.get("market_temperature") or {}
    lu, ld = mt.get("limit_up_count"), mt.get("limit_down_count")
    inflow = mt.get("top_inflow_sectors") or []
    if isinstance(lu, int) and isinstance(ld, int):
        src_bits.append(f"**涨跌停统计**（尽力探测）：涨停 {lu} 家 / 跌停 {ld} 家")
    elif isinstance(lu, int) or isinstance(ld, int):
        lu_s = f"{lu} 家" if isinstance(lu, int) else "暂缺"
        ld_s = f"{ld} 家" if isinstance(ld, int) else "暂缺"
        src_bits.append(f"**涨跌停统计**：涨停 {lu_s} / 跌停 {ld_s}")
    if isinstance(inflow, list) and inflow:
        ip = []
        for row in inflow[:3]:
            if not isinstance(row, dict):
                continue
            nm = str(row.get("name") or "").strip()
            v = row.get("main_net_inflow_yi")
            if nm and isinstance(v, (int, float)):
                ip.append(f"{nm}({v:+.2f}亿)")
            elif nm:
                ip.append(nm)
        if ip:
            src_bits.append("**主力净流入行业**（探测）：" + "、".join(ip))

    sent = m.get("market_sentiment") or {}
    kws = sent.get("hot_keywords") or []
    stks = sent.get("top_hot_stocks") or []
    if isinstance(kws, list) and kws:
        src_bits.append("**市场热词**（尽力探测）：" + "、".join(str(x) for x in kws[:6]))
    if isinstance(stks, list) and stks:
        src_bits.append(
            "**人气股**（尽力探测）："
            + "、".join(f"{s.get('name')}({s.get('code')})" for s in stks if isinstance(s, dict))
        )

    mood_tail = ""
    has_ext = (
        nb_val is not None
        or (isinstance(ir, list) and len(ir) > 0)
        or isinstance(lu, int)
        or isinstance(ld, int)
        or (isinstance(inflow, list) and len(inflow) > 0)
        or (isinstance(kws, list) and len(kws) > 0)
        or (isinstance(stks, list) and len(stks) > 0)
    )
    if not has_ext:
        mood_tail = "\n\n> 今日资金与情绪扩展字段多为暂缺（已尝试 AkShare/东财相关接口）。三大指数仍以新浪为准。"

    block_market = "\n".join(f"- {x}" for x in src_bits) + mood_tail

    by_sec = n.get("items_by_sector") or {}
    other_flash_items = n.get("items_other_flash") or []
    macro_items_raw = macro_sec.get("items") or []
    deep_sec = sections.get("deep_news") or {}
    deep_items_for_sectors: list[dict[str, Any]] = deep_sec.get("items") or []
    gm_sec = sections.get("global_macro") or {}
    gm_items: list[dict[str, Any]] = gm_sec.get("items") or []

    # F3/T3：优先走 LLM Router 的板块重组；失败/不可用时自动回退 legacy 规则聚合
    llm_router_sec = sections.get("llm_router") or {}
    llm_router_ok = str(llm_router_sec.get("status") or "") == "ok"
    llm_items_by_sec = llm_router_sec.get("items_by_sector") or {}
    if llm_router_ok and isinstance(llm_items_by_sec, dict):
        enriched_by_sec = {sec: list(llm_items_by_sec.get(sec) or []) for sec in SECTOR_ORDER}
    else:
        enriched_by_sec = _legacy_sector_enriched(
            by_sec,
            gm_items,
            deep_items_for_sectors,
            list(other_flash_items),
            list(macro_items_raw),
        )

    sector_lines: list[str] = []
    for sec in SECTOR_ORDER:
        sector_lines.append(f"**【{sec}】**")
        raw_sec_items = enriched_by_sec.get(sec) or []
        # 板块内按标题去重（保留正文最长的版本），再取 top-5
        _sec_seen: dict[str, dict[str, Any]] = {}
        for _it in raw_sec_items:
            if not isinstance(_it, dict):
                continue
            _title_raw = _clean_display_text(str(_it.get("title") or ""))
            if (
                _title_raw.startswith("回复 ")
                or _title_raw.startswith("回复周家旭")
                or _title_raw in {"拉取今日讯息", "今日讯息", "拉取热点", "热点"}
            ):
                continue
            _k = _title_dedup_key(str(_it.get("title") or ""))
            if not _k:
                continue
            if _k not in _sec_seen:
                _sec_seen[_k] = _it
            else:
                _existing = _sec_seen[_k]
                _body_new = len(str(_it.get("clean_text") or _it.get("summary") or ""))
                _body_old = len(str(_existing.get("clean_text") or _existing.get("summary") or ""))
                if _body_new > _body_old:
                    _sec_seen[_k] = _it
        sec_items = list(_sec_seen.values())
        sec_items.sort(
            key=lambda x: (
                _sector_source_rank(x, sec),
                _parse_published_at(str(x.get("published_at") or "")) or datetime.min.replace(tzinfo=timezone(timedelta(hours=8))),
            ),
            reverse=True,
        )
        sec_items = sec_items[:5]
        out_n = 0
        for it in sec_items:
            if not isinstance(it, dict):
                continue
            ts_full = str(it.get("published_at") or "").strip()
            ts_display = ts_full[:19].replace("T", " ")
            hh = news_rss.news_hhmm_for_markdown(ts_full)
            tpart = f"[{ts_display}]" if len(ts_full) >= 16 else f"[{hh}]"
            title = _clean_display_text(str(it.get("title") or ""))
            body = _clip_flash_text(
                _clean_display_text(str(it.get("clean_text") or it.get("summary") or it.get("title") or "")),
                max_len=120,
            )
            if not llm_router_ok:
                if not _sector_strong_match(
                    sec,
                    title,
                    body,
                    item_tags=it.get("sector_tags") or [],
                    vertical_target_sector=str(it.get("vertical_target_sector") or ""),
                ):
                    continue
            # 情绪标注
            s_emoji = str(it.get("sentiment_emoji") or "").strip()
            s_hint = str(it.get("sentiment_hint") or "").strip()
            if not s_hint:
                _txt = f"{title} {body}"
                s_hint = classify_sentiment(_txt)
                s_emoji = _s_emoji(s_hint)
            s_prefix = f"{s_emoji}{s_hint} " if s_emoji and s_hint else ""
            # 来源标注
            src_label = _item_source_label(it)
            src_suffix = f" _[{src_label}]_" if src_label else ""
            # 特殊标注（双源共振/关键词回溯）
            src = str(it.get("sector_line_source") or "")
            extra_note = ""
            if src == "recent_keyword":
                extra_note = " *〔关键词回溯〕*"
            elif src == "tagged_catchup":
                extra_note = " *〔同板块补位〕*"
            if it.get("cross_source_hit"):
                extra_note += " *〔双源共振〕*"
            if title or body:
                if title and body and not body.startswith(title[:min(10, len(title))]):
                    line = f"- {s_prefix}{tpart} **{title}** — {body}{extra_note}{src_suffix}"
                elif title:
                    line = f"- {s_prefix}{tpart} **{title}**{extra_note}{src_suffix}"
                else:
                    line = f"- {s_prefix}{tpart} {body}{extra_note}{src_suffix}"
                sector_lines.append(line)
                out_n += 1
        used_llm_empty_state = False
        if llm_router_ok and out_n == 0:
            sector_lines.append(
                "- 🧠 **深度洞察**：今日该板块暂无显著的超预期事件驱动或高价值资讯，盘面主要受宏观大盘资金面主导。"
            )
            out_n += 1
            used_llm_empty_state = True
        if not used_llm_empty_state and out_n < 3:
            for fill_line in _market_sector_fill_lines(sec, m, min_count=3 - out_n):
                sector_lines.append(fill_line)
                out_n += 1
                if out_n >= 3:
                    break

    all_news_items: list[dict[str, Any]] = []
    for bucket in [n.get("items") or [], n.get("items_other_flash") or [], gm_items, deep_items_for_sectors]:
        if isinstance(bucket, list):
            all_news_items.extend([x for x in bucket if isinstance(x, dict)])

    major_lines = _select_weekly_major_lines(
        all_news_items,
        macro_sec.get("items") or [],
        fetched_at=fetched_at,
        limit=5,
    )
    if not major_lines:
        major_lines.append("- （近 7 日未筛出足够高重要度的国家/全球/政策事件；可结合板块快讯自行核对）")

    # 与大事件口径对齐的去重 key（用于热点去重）
    now_dt = _parse_published_at(fetched_at) or datetime.now(timezone(timedelta(hours=8)))
    week_start = now_dt - timedelta(days=7)
    major_event_items_for_dedup: list[dict[str, Any]] = []
    scored_for_major: list[tuple[float, datetime, dict[str, Any]]] = []
    for it in all_news_items:
        if not isinstance(it, dict):
            continue
        if not _is_finance_related(it):
            continue
        dt = _parse_published_at(str(it.get("published_at") or ""))
        if dt is None or dt < week_start:
            continue
        s = _major_event_score(it, now_dt)
        if s < 2.5:
            continue
        scored_for_major.append((s, dt, it))
    scored_for_major.sort(key=lambda x: (x[0], x[1]), reverse=True)
    seen_theme: set[str] = set()
    seen_title: set[str] = set()
    for _, _, it in scored_for_major:
        if not _is_major_event_whitelisted(it):
            continue
        if _is_corporate_actor_event(it):
            continue
        theme = _major_event_theme_key(it)
        norm = str(it.get("title") or "").strip()[:36]
        if theme in seen_theme:
            continue
        if norm and norm in seen_title:
            continue
        if norm:
            seen_title.add(norm)
        seen_theme.add(theme)
        major_event_items_for_dedup.append(it)
        if len(major_event_items_for_dedup) >= 5:
            break

    # F2: 今日热点讯息 — 全信源池化，筛最重要 5 条（排除六大板块和大事件已有内容）
    other = other_flash_items
    hotspot_items = _build_hotspot_top5(
        gm_items,
        deep_items_for_sectors,
        list(other),
        list(macro_items_raw),
        enriched_by_sec,
        major_event_items_for_dedup,
    )
    hotspot_lines: list[str] = []
    for it in hotspot_items:
        s_emoji = str(it.get("sentiment_emoji") or "").strip()
        s_hint = str(it.get("sentiment_hint") or "").strip()
        s_prefix = f"{s_emoji}{s_hint} " if s_emoji and s_hint else ""
        src_label = _item_source_label(it)
        src_suffix = f" _[{src_label}]_" if src_label else ""
        ts_full = str(it.get("published_at") or "").strip()
        ts_display = ts_full[:19].replace("T", " ")
        tpart = f"[{ts_display}]" if len(ts_full) >= 16 else ""
        title = _clean_display_text(str(it.get("title") or ""))
        body = _clip_flash_text(
            _clean_display_text(str(it.get("clean_text") or it.get("summary") or it.get("title") or "")),
            max_len=200,
        )
        if title and body and not body.startswith(title[:min(10, len(title))]):
            line = f"- {s_prefix}{tpart} **{title}** — {body}{src_suffix}"
        elif title:
            line = f"- {s_prefix}{tpart} **{title}**{src_suffix}"
        else:
            line = f"- {s_prefix}{tpart} {body}{src_suffix}"
        if line.strip() != "-":
            hotspot_lines.append(line)
    if not hotspot_lines:
        hotspot_lines.append("- （今日热点本轮无可用条目）")

    social_lines: list[str] = []
    s_items = soc.get("items") or []
    if isinstance(s_items, list):
        for it in s_items[:6]:
            if not isinstance(it, dict):
                continue
            title = _clean_display_text(str(it.get("title") or ""))
            plat = (it.get("platform") or "").strip()
            detail = _clip_flash_text(_clean_display_text(str(it.get("clean_text") or "")), max_len=140)
            title_norm = title.replace("\xa0", " ").strip()
            if (
                title_norm.startswith("回复 ")
                or title_norm.startswith("回复周家旭")
                or title_norm in {"拉取今日讯息", "今日讯息", "拉取热点", "热点"}
            ):
                continue
            if title:
                line = f"- [{plat}] **{title}**" if plat else f"- **{title}**"
                if detail:
                    line += f"｜概述：{detail}"
                social_lines.append(line)
    if not social_lines:
        social_lines.append("- （社媒/人气榜 API 暂不可用或本轮无条目）")

    error_lines: list[str] = []
    router_status = str((sections.get("llm_router") or {}).get("status") or "")
    if router_status == "ok":
        router_diag = "💡 数据引擎：已启用 LLM 深度去噪与提纯"
    else:
        router_diag = "⚠️ 数据引擎：LLM 路由超时/异常，已平滑降级为词典匹配模式"
    error_cn = {
        "TGB_HOT_FAILED": "淘股吧/社区热榜接口不可用。",
        "WC_RANK_FAILED": "问财/人气榜接口不可用。",
        "EM_RANK_FAILED": "东方财富人气榜接口异常。",
        "MACRO_HOT_FINANCE_FILTER_EMPTY": "百度实时热榜未筛出可靠财经条目。",
        "SOCIAL_WB_HOT_FAILED": "微博热搜接口不可用。",
        "SOCIAL_TGB_OR_HOT_RANK_FAILED": "第二梯队社媒/人气榜接口不可用。",
        "BAIDU_HOT_FAILED": "百度实时热搜抓取失败，社媒舆情需其他渠道补充。",
        "BAIDU_HOT_NO_MATCH": "百度热搜未命中宏观/金融关键词。",
        "SINA_LIVE_FEED_FAILED": "新浪财经7x24接口不可用。",
        "SINA_LIVE_FINANCE_FILTER_EMPTY": "新浪7x24有数据但未命中金融关键词过滤。",
        "CSRC_INDEX_FAILED": "证监会公告列表抓取失败。",
        "PBC_INDEX_FAILED": "人民银行公告列表抓取失败。",
        "SINA_HK_INDICES_FAILED": "港股指数接口不可用。",
        "NORTHBOUND_PROBE_FAILED": "北向资金 AkShare 接口不可用。",
        "NORTHBOUND_TUSHARE_FAILED": "北向资金 Tushare 降级接口失败。",
        "NORTHBOUND_SINA_TEXT_FAILED": "新浪7x24 北向文本降级失败。",
        "NORTHBOUND_RSSHUB_TEXT_FAILED": "RSSHub 北向文本降级失败。",
        "NEWS_RSSHUB_BASE_URL_MISSING": "未配置 FINANCE_RSSHUB_BASE_URL，六大板块快讯为空。",
        "NEWS_RSSHUB_ROUTES_FAILED": "RSSHub 新闻路由全部失败（超时/网络/解析）。",
        "NEWS_RSSHUB_FILTER_EMPTY": "RSSHub 有数据但未命中快讯关键词过滤。",
        "NEWS_FEEDPARSER_IMPORT_ERROR": "缺少 feedparser 依赖，无法解析 RSS。",
        "CLS_AKSHARE_IMPORT_FAILED": "AkShare 财联社快讯模块不可用（仅 RSSHub 生效）。",
        "NEWS_RSSHUB_REPAIR_SUGGESTED": "可授权 Agent 执行 RSSHub 更新脚本后重试（支持飞书/微信确认）。",
        "WALLSTREETCN_API_EMPTY": "华尔街见闻 API 返回空条目（直连路径）。",
    }
    error_lines.append(f"**告警（中文说明，最多 6 条）**（{len(errors)}）：")
    error_lines.append(f"- {router_diag}")
    if errors:
        for e in errors[:6]:
            code = str(e.get("code") or "")
            msg = error_cn.get(code) or str(e.get("message") or "接口调用失败")
            error_lines.append(f"- **{code}**：{msg}")

    markdown = f"""## 📊 今日信源全量快照 ({fetched_at})

### 【📈 大盘与情绪】
{block_market}

### 【🎯 核心板块异动】（全信源 · 六大板块精选 3-5 条 · 情绪+来源标注）
{chr(10).join(sector_lines)}

### 【🧭 大事件】（近 7 日高重要度 · 国家/全球/政策/地缘）
{chr(10).join(major_lines)}

### 【🔥 今日热点讯息】（非六大板块 · 全信源精选 5 条）
{chr(10).join(hotspot_lines)}

**社媒 / 人气榜（探测）**
{chr(10).join(social_lines)}
"""
    # v0.1.9：不再单独渲染「深度内容」Markdown 区；`sections.deep_news` 仍输出，
    # 且已并入上方「核心板块异动」等逻辑（见 _enrich_sectors_from_all_sources）。

    if error_lines:
        markdown += "\n" + "\n".join(error_lines) + "\n"

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
    _rb = os.environ.get("FINANCE_RSSHUB_BASE_URL", "").strip().rstrip("/")
    if _rb:
        meta_extra["finance_rsshub_base_url"] = _rb

    macro_data, macro_errs = macro_hot.fetch_macro_section(limit=12)
    sections["macro_hot"] = macro_data
    errors.extend(macro_errs)
    if macro_data.get("items"):
        sources_ok.append("macro_hot")

    sina_live_data, sina_live_errs = news_sina_live.fetch_sina_live_section(12)
    errors.extend(sina_live_errs)
    policy_data, policy_errs = policy_gov.fetch_policy_section()
    errors.extend(policy_errs)
    merged_global: list[dict[str, Any]] = []
    for _it in sina_live_data.get("items") or []:
        if isinstance(_it, dict):
            merged_global.append(dict(_it))
    for _it in policy_data.get("items") or []:
        if isinstance(_it, dict):
            merged_global.append(dict(_it))
    merged_global.sort(key=lambda z: str(z.get("published_at") or ""), reverse=True)
    sections["global_macro"] = {
        "items": merged_global,
        "sina_live": sina_live_data,
        "policy": policy_data,
    }
    if sina_live_data.get("items"):
        sources_ok.append("global_macro:sina_live")
    if policy_data.get("items"):
        sources_ok.append("global_macro:policy")

    if "news" in sources:
        data, errs = news_rss.fetch_news_section(keywords, max_items)
        if _news_items_empty(data):
            if not _rsshub_self_heal_enabled():
                logger.warning("[Agent 诊断] FINANCE_RSSHUB_SELF_HEAL=0，已禁用自动修复。")
                meta_extra["news_rsshub_self_heal"] = "disabled_by_env"
                _append_rsshub_manual_repair_notice(errs, meta_extra, reason="disabled_by_env")
            elif _prompt_rsshub_self_heal():
                if _run_rsshub_update_script():
                    retry_data, retry_errs = news_rss.fetch_news_section(keywords, max_items)
                    data = retry_data
                    errs = [*errs, *retry_errs]
                    if _news_items_empty(data):
                        meta_extra["news_rsshub_self_heal"] = "updated_but_still_empty"
                        _append_rsshub_manual_repair_notice(errs, meta_extra, reason="updated_but_still_empty")
                    else:
                        meta_extra["news_rsshub_self_heal"] = "updated_and_retried"
                else:
                    meta_extra["news_rsshub_self_heal"] = "update_failed"
                    _append_rsshub_manual_repair_notice(errs, meta_extra, reason="update_failed")
            else:
                meta_extra["news_rsshub_self_heal"] = "skipped"
                _append_rsshub_manual_repair_notice(errs, meta_extra, reason="user_skipped_or_non_interactive")
        sections["news"] = data
        errors.extend(errs)
        sources_ok.append("news")
        if data.get("keyword_fallback"):
            meta_extra["news_keyword_fallback"] = True
        if data.get("rsshub_paths_ok") is not None:
            meta_extra["news_rsshub_paths_ok"] = data.get("rsshub_paths_ok") or []
        if data.get("cls_source_ok") is not None:
            meta_extra["news_cls_source_ok"] = bool(data.get("cls_source_ok"))
        if data.get("sector_filter_fallback"):
            meta_extra["news_sector_filter_fallback"] = True
        if data.get("sector_relax_backfill"):
            meta_extra["news_sector_relax_backfill"] = True

        # 东财热榜关闭时：从已拉取的快讯标题离线抽词填充 sentiment，便于下游可选展示（不产生告警项）
        if "market_sentiment" in sections.get("market", {}):
            sentiment = sections["market"]["market_sentiment"]
            news_items = data.get("items") or []
            if not sentiment.get("hot_keywords") and not sentiment.get("top_hot_stocks") and news_items:
                extracted_kws = market.extract_keywords_from_news(news_items)
                sections["market"]["market_sentiment"]["hot_keywords"] = extracted_kws
                sections["market"]["market_sentiment"]["hot_keywords_source"] = "offline_news_extract"
                if extracted_kws:
                    sections["market"]["market_sentiment"]["note"] = "热词来自快讯离线抽取（社区/东财热榜未返回时的兜底）"

    skip_social = truthy_env("FINANCE_SOURCE_SKIP_SOCIAL")
    if "social" in sources and skip_social:
        meta_extra["social_skipped"] = True

    if "social" in sources and not skip_social:
        data, errs = social_api.fetch_social_section(keywords, max_items)
        sections["social"] = data
        errors.extend(errs)
        sources_ok.append("social")
        if data.get("tier_used") is not None:
            meta_extra["social_tier_used"] = data.get("tier_used")
        if data.get("source_primary"):
            meta_extra["social_source_primary"] = data.get("source_primary")
        meta_extra["social_scrape_stub"] = social_scrape_stub.fetch_social_scrape_stub()

    # --- 深度内容层（华尔街见闻 / 第一财经 / 界面新闻） ---
    _dn_data, _dn_errs = deep_news.fetch_deep_news_section(limit=8)
    sections["deep_news"] = _dn_data
    errors.extend(_dn_errs)
    if _dn_data.get("sources_ok"):
        sources_ok.append("deep_news:" + "+".join(_dn_data["sources_ok"]))
    meta_extra["deep_news_sources_ok"] = _dn_data.get("sources_ok") or []
    if _dn_data.get("rsshub_base_url"):
        meta_extra["deep_news_rsshub_base_url"] = _dn_data["rsshub_base_url"]
    if _dn_data.get("sector_rsshub_matrix"):
        meta_extra["deep_news_sector_rsshub"] = _dn_data["sector_rsshub_matrix"]

    # --- CLS 快讯情感回填（非破坏性，仅在字段缺失时追加）---
    for _bucket in ("items", "items_other_flash"):
        for _it in (sections.get("news") or {}).get(_bucket) or []:
            if not isinstance(_it, dict) or "sentiment_hint" in _it:
                continue
            _txt = f"{_it.get('title') or ''} {_it.get('clean_text') or ''}"
            _s = classify_sentiment(_txt)
            _it["sentiment_hint"] = _s
            _it["sentiment_emoji"] = _s_emoji(_s)
            _it["impact_level"] = classify_impact(_txt)
            _it["stock_mentions"] = extract_stock_mentions(_txt)

    # --- LLM Router：菜单路由六大板块（失败自动回退 legacy） ---
    _news_sec = sections.get("news") or {}
    _macro_sec = sections.get("macro_hot") or {}
    _gm_sec = sections.get("global_macro") or {}
    _deep_sec = sections.get("deep_news") or {}
    _router_items_by_sec, _router_meta = _build_sector_items_with_router(
        _news_sec.get("items_by_sector") or {},
        _gm_sec.get("items") or [],
        _deep_sec.get("items") or [],
        _news_sec.get("items_other_flash") or [],
        _macro_sec.get("items") or [],
        errors,
    )
    sections["llm_router"] = {
        "status": _router_meta.get("status") or "fallback_legacy",
        "items_by_sector": _router_items_by_sec,
        "menu_count": _router_meta.get("menu_count") or 0,
        "selected_count": _router_meta.get("selected_count") or 0,
        "reason": _router_meta.get("reason") or "",
    }
    meta_extra["llm_router_status"] = sections["llm_router"]["status"]
    if sections["llm_router"]["reason"]:
        meta_extra["llm_router_reason"] = sections["llm_router"]["reason"]
    if sections["llm_router"]["menu_count"]:
        meta_extra["llm_router_menu_count"] = sections["llm_router"]["menu_count"]
    if sections["llm_router"]["selected_count"]:
        meta_extra["llm_router_selected_count"] = sections["llm_router"]["selected_count"]

    md = _build_live_stream_markdown(sections, errors, fetched_at)
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
