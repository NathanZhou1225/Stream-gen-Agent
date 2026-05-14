"""编排各 fetcher，生成统一 JSON + markdown_summary。

本模块为 **legacy 全量快照**（`ingest.py legacy` → `build_snapshot`）服务。

**LLM Router / 板块润色 / DB 飞书 `markdown_summary`（v0.2.2+）**：
已迁至 `workspace-stream-gen/skills/finance-draft-manager/`（`router.py` /
`rewriter.py` / `db_snapshot.py`）。飞书默认拉数路径为 `query_market_facts.py`
读库 + `db_snapshot`，**不要**在本文件继续扩展 Router 能力；以下 `_router_*`
仅保留给 legacy 输出兼容与历史 cron 仍调 legacy 的场景。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import re
import select
import ssl
import subprocess
import sys
import time
from pathlib import Path
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
from fetchers.social_intelligence import enhance_social_intelligence

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


_COLLECTION_TITLE_HINTS: tuple[str, ...] = (
    "早报",
    "早餐",
    "要闻全览",
    "盘前要点",
    "盘前必读",
    "环球市场",
    "晨会精华",
    "FM-Radio",
    "音频",
)

_SECTOR_ANCHOR_TERMS: dict[str, tuple[str, ...]] = {
    "科技": (
        "OpenAI",
        "英伟达",
        "GPU",
        "AI芯片",
        "人工智能",
        "AI",
        "算力",
        "芯片",
        "半导体",
        "大模型",
        "光模块",
        "CPO",
        "数据中心",
        "服务器",
        "机器人",
        "具身智能",
        "云计算",
        "存储",
    ),
    "新能源": (
        "新能源",
        "光伏",
        "风电",
        "储能",
        "锂电",
        "电池",
        "充电桩",
        "氢能",
        "新能源车",
        "电动车",
        "电力",
        "电网",
        "绿电",
        "智能电网",
        "微电网",
        "固态变压器",
        "碳酸锂",
        "锂价",
        "锂矿",
        "电池材料",
        "正极材料",
        "负极材料",
    ),
    "港股": ("港股", "恒生", "恒生科技", "南向", "北水", "港股通", "港交所", "中资股", "H股"),
    "黄金": (
        "黄金",
        "金价",
        "现货黄金",
        "COMEX",
        "贵金属",
        "纽约期金",
        "沪金",
        "黄金ETF",
        "金银比",
        "伦敦金",
        "央行购金",
        "央行增持黄金",
        "避险",
        "地缘",
        "冲突",
        "美联储",
        "降息",
    ),
    "有色": (
        "有色",
        "能源金属",
        "工业金属",
        "铜",
        "铝",
        "锌",
        "镍",
        "钴",
        "锂",
        "稀土",
        "锂矿",
        "伦铜",
        "沪铜",
        "LME",
        "氧化铝",
        "电解铝",
        "碳酸锂",
        "锂价",
        "小金属",
        "矿端",
        "库存",
    ),
    "银行": (
        "银行",
        "信贷",
        "息差",
        "净息差",
        "不良率",
        "拨备",
        "存款",
        "贷款",
        "LPR",
        "降准",
        "同业存款",
        "Shibor",
        "金融监管",
    ),
}

_TECH_WEAK_NEGATIVE_TERMS: tuple[str, ...] = (
    "爱奇艺",
    "剧集",
    "综艺",
    "长视频",
    "文娱",
    "票房",
    "影视",
)

_GOLD_FALSE_POSITIVE_TERMS: tuple[str, ...] = (
    "黄金时代",
    "黄金赛道",
    "黄金十年",
    "黄金窗口",
    "黄金期",
    "黄金周",
    "黄金档",
    "黄金地段",
    "商业航天",
)

_BANK_BUSINESS_TERMS: tuple[str, ...] = (
    "商业银行",
    "银行股",
    "股份行",
    "城商行",
    "农商行",
    "信贷",
    "息差",
    "净息差",
    "不良率",
    "拨备",
    "存款",
    "贷款",
    "LPR",
    "降准",
    "同业存款",
    "Shibor",
    "银行监管",
    "资本充足率",
)

_BANK_FALSE_POSITIVE_TERMS: tuple[str, ...] = (
    "信息差",
    "认知差",
    "智商税",
    "白桦树汁",
    "果汁",
    "饮料",
)

_NEGATIVE_FINANCE_TERMS: tuple[str, ...] = (
    "债务危机",
    "债务逾期",
    "账户被冻结",
    "司法冻结",
    "被诉",
    "诉讼",
    "亏损",
    "利润暴跌",
    "退市",
    "ST",
    "破产",
    "违约",
)

_VAGUE_DISPLAY_TERMS: tuple[str, ...] = (
    "可能",
    "大概",
    "或许",
    "似乎",
    "或将",
    "有望",
    "预计",
)


def _is_collection_title(title: str) -> bool:
    return any(k in (title or "") for k in _COLLECTION_TITLE_HINTS)


def _sector_anchor_hits(sec: str, title: str, body: str) -> list[str]:
    blob = f"{title} {body}"
    return [k for k in _SECTOR_ANCHOR_TERMS.get(sec, ()) if k and k in blob]


def _has_negative_finance_terms(text: str) -> bool:
    return any(k in (text or "") for k in _NEGATIVE_FINANCE_TERMS)


def _sector_focus_terms(sec: str) -> tuple[str, ...]:
    extra: dict[str, tuple[str, ...]] = {
        "黄金": ("央行", "购金", "增持", "黄金储备", "金价", "现货黄金", "COMEX", "贵金属", "地缘", "冲突", "中东", "伊朗", "避险", "美联储", "降息"),
        "有色": ("铜", "铝", "锂", "镍", "稀土", "碳酸锂", "锂价", "库存", "矿端", "供给", "需求"),
    }
    return tuple(dict.fromkeys((*_SECTOR_ANCHOR_TERMS.get(sec, ()), *extra.get(sec, ()))))


def _sector_focused_sentence(sec: str, text: str, *, max_len: int = 150) -> str:
    terms = _sector_focus_terms(sec)
    if not terms:
        return ""
    sentences = re.split(r"[。！？；;\n]\s*", text or "")
    picked: list[str] = []
    for sent in sentences:
        clean = _clean_display_text(sent)
        if len(clean) < 8:
            continue
        if sec in {"黄金", "有色", "新能源"} and any(k in clean for k in _VAGUE_DISPLAY_TERMS):
            continue
        if any(k in clean for k in terms):
            picked.append(clean)
        if len("；".join(picked)) >= max_len or len(picked) >= 2:
            break
    return _clip_flash_text("；".join(picked), max_len=max_len) if picked else ""


def _sector_item_is_usable(sec: str, it: dict[str, Any]) -> bool:
    """板块可用性底线：进入展示前必须有可解释的行业/商品锚点。"""
    title = _clean_display_text(str(it.get("title") or ""))
    body = _clean_display_text(str(it.get("clean_text") or it.get("summary") or it.get("detail") or ""))
    anchors = _sector_anchor_hits(sec, title, body)
    if not anchors:
        return False
    if _is_collection_title(title) and len(body) < 40:
        return False
    blob = f"{title} {body}"
    if sec == "科技" and any(k in f"{title} {body}" for k in _TECH_WEAK_NEGATIVE_TERMS):
        # 文娱/平台类文章只有在同时具备强科技锚点时才进科技板块。
        strong = {"OpenAI", "英伟达", "GPU", "AI芯片", "人工智能", "AI", "算力", "芯片", "半导体", "CPO", "大模型"}
        title_anchors = set(_sector_anchor_hits(sec, title, ""))
        return any(a in strong for a in title_anchors)
    if sec == "黄金":
        if any(k in blob for k in _GOLD_FALSE_POSITIVE_TERMS):
            return any(k in blob for k in ("金价", "现货黄金", "COMEX", "贵金属", "央行购金", "黄金储备", "黄金ETF", "沪金", "纽约期金"))
        return bool(anchors)
    if sec == "有色":
        return bool(anchors)
    if sec == "银行":
        if any(k in blob for k in _BANK_FALSE_POSITIVE_TERMS):
            return False
        # “人民银行/央行”本身不等于银行板块，需有商业银行业务或监管锚点。
        return any(k in blob for k in _BANK_BUSINESS_TERMS)
    return True


def _candidate_sectors_for_item(it: dict[str, Any], *, max_sectors: int = 2) -> list[str]:
    hits: list[str] = []
    for sec in _ROUTER_SECTORS:
        if _sector_item_is_usable(sec, it):
            hits.append(sec)
    if "科技" in hits and "新能源" in hits:
        ordered = ["科技", "新能源"] + [x for x in hits if x not in {"科技", "新能源"}]
        return ordered[:max_sectors]
    return hits[:max_sectors]


def _annotate_item_for_sector(it: dict[str, Any], sec: str, *, line_source: str | None = None) -> dict[str, Any]:
    out = dict(it)
    sectors = _candidate_sectors_for_item(out, max_sectors=2)
    if sec not in sectors:
        sectors.insert(0, sec)
    out["primary_sector"] = sectors[0] if sectors else sec
    out["related_sectors"] = [s for s in sectors if s != out["primary_sector"]]
    out["display_sector"] = sec
    anchors = _sector_anchor_hits(
        sec,
        str(out.get("title") or ""),
        str(out.get("clean_text") or out.get("summary") or out.get("detail") or ""),
    )
    out["sector_reason"] = "、".join(anchors[:4]) if anchors else sec
    if line_source and not str(out.get("sector_line_source") or "").strip():
        out["sector_line_source"] = line_source
    return out


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


# ── LEGACY LLM Router（仅 `build_snapshot` / `ingest.py legacy`）──────────────
# 新默认链路的点菜与润色见 finance-draft-manager；此处代码勿作新功能扩展面。
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


def _social_intel_item_dedupe_key(it: dict[str, Any]) -> str:
    """社交情报汇总前去重：优先稳定 id/url，其次路由风格标题键 + 时间 + 来源。"""
    if not isinstance(it, dict):
        return ""
    for k in ("id", "link", "url", "guid"):
        v = it.get(k)
        if v is not None and str(v).strip():
            return f"{k}:{str(v).strip()}"
    pub = str(it.get("published_at") or it.get("pub_time") or it.get("date") or "")
    src = str(it.get("source") or it.get("platform") or "")
    dk = _router_event_dedup_key(it)
    return f"h:{src}|{pub}|{dk}"


def _dedupe_social_intel_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        k = _social_intel_item_dedupe_key(it)
        if not k:
            k = f"anon:{id(it)}"
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def _workspace_stream_gen_root() -> Path:
    """``pipeline.py`` 位于 ``…/skills/finance-source-ingest/scripts/``。"""
    return Path(__file__).resolve().parent.parent.parent.parent


def _finance_db_path_for_social_hist() -> Path:
    env = os.environ.get("FINANCE_DB_PATH", "").strip()
    if env:
        return Path(env)
    return _workspace_stream_gen_root() / "user_data" / "finance_sources.db"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)).strip())
    except ValueError:
        return default


def _dedupe_router_items_across_sectors(
    items_by_sec: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """跨板块事件仲裁：普通事件单主归属，强联动白名单允许最多双归属。"""
    out: dict[str, list[dict[str, Any]]] = {s: [] for s in _ROUTER_SECTORS}
    grouped: dict[str, list[tuple[str, int, dict[str, Any]]]] = {}
    for si, sec in enumerate(_ROUTER_SECTORS):
        for it in items_by_sec.get(sec) or []:
            if not isinstance(it, dict):
                continue
            dk = _router_event_dedup_key(it)
            if not dk or len(dk) < 8:
                out[sec].append(it)
                continue
            grouped.setdefault(dk, []).append((sec, si, it))

    for _dk, entries in grouped.items():
        if len(entries) == 1:
            sec, _si, it = entries[0]
            out[sec].append(it)
            continue
        ranked = sorted(
            entries,
            key=lambda x: (_router_sector_claim_score(x[0], x[2]), len(str(x[2].get("clean_text") or x[2].get("summary") or "")), -x[1]),
            reverse=True,
        )
        kept: list[tuple[str, int, dict[str, Any]]] = [ranked[0]]
        for cand in ranked[1:]:
            if len(kept) >= 2:
                break
            if _router_allowed_cross_sector_pair(kept[0][0], cand[0], cand[2]):
                kept.append(cand)
                break
        for sec, _si, it in kept:
            out[sec].append(it)
    return out


def _router_item_blob(it: dict[str, Any]) -> str:
    return f"{it.get('title') or ''} {it.get('clean_text') or it.get('summary') or ''}".lower()


def _router_sector_claim_score(sec: str, it: dict[str, Any]) -> int:
    score = 0
    if str(it.get("vertical_target_sector") or "").strip() == sec:
        score += 80
    tags = {str(x).strip() for x in (it.get("sector_tags") or []) if str(x).strip()}
    if sec in tags:
        score += 40
    if str(it.get("candidate_sector") or "").strip() == sec:
        score += 20
    if _sector_strong_match(
        sec,
        str(it.get("title") or ""),
        str(it.get("clean_text") or it.get("summary") or ""),
        item_tags=it.get("sector_tags") or [],
        vertical_target_sector=str(it.get("vertical_target_sector") or ""),
    ):
        score += 10
    score += _depth_source_rank(it)
    return score


def _router_allowed_cross_sector_pair(sec_a: str, sec_b: str, it: dict[str, Any]) -> bool:
    pair = {sec_a, sec_b}
    txt = _router_item_blob(it)

    def has_any(words: tuple[str, ...]) -> bool:
        return any(w.lower() in txt for w in words)

    if pair == {"港股", "科技"}:
        return has_any(("港股", "恒生", "港股通", "港交所", "中资股")) and has_any(
            ("芯片", "半导体", "AI", "人工智能", "互联网", "算力", "机器人")
        )
    if pair == {"黄金", "有色"}:
        return has_any(("黄金", "金价", "贵金属", "白银", "期金", "期银")) and has_any(
            ("有色", "铜", "铝", "锌", "镍", "金属", "矿", "白银")
        )
    if pair == {"银行", "港股"}:
        return has_any(("央行", "降准", "降息", "货币政策", "金融监管")) and has_any(
            ("银行", "信贷", "息差", "贷款", "存款")
        ) and has_any(("港股", "恒生", "中资股", "港股通"))
    if pair == {"新能源", "科技"}:
        return has_any(("储能", "电池", "光伏", "新能源车", "电网", "智能驾驶")) and has_any(
            ("芯片", "AI", "人工智能", "机器人", "算力", "智能驾驶")
        )
    return False


def _router_keyword_strong_match(sec: str, title: str, body: str) -> bool:
    """不吃标签/垂直路由豁免的强相关判断，用于宽频道与降级输出。"""
    return _sector_item_is_usable(sec, {"title": title, "clean_text": body})


def _router_source_requires_strict_match(it: dict[str, Any]) -> bool:
    """宽频道/宽池来源不能仅凭 vertical_target_sector 直接进入板块。"""
    src = str(it.get("source_name") or "")
    route_key = str(it.get("deep_route_key") or "")
    line_src = str(it.get("sector_line_source") or "")
    if line_src in {"global_macro", "macro_hot", "other_flash"}:
        return True
    if route_key.endswith("_fb") or "xueqiu" in route_key.lower():
        return True
    return any(k in src for k in ("格隆汇首页", "36氪科技", "财联社深度", "华尔街见闻最热", "雪球热帖", "宽池"))


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
    blob = f"{title} {body}"
    pseudo_item = {
        "title": title,
        "clean_text": body,
        "sector_tags": list(item_tags or []),
        "vertical_target_sector": vertical_target_sector or "",
    }
    if not _sector_item_is_usable(sec, pseudo_item):
        return False
    # 银行板块先做反向排除，避免被标签豁免误放行
    if sec == "银行":
        analyst_terms = ("目标价", "评级")
        tech_subject_terms = (
            "半导体",
            "芯片",
            "arm",
            "阿斯麦",
            "asml",
            "英伟达",
            "台积电",
            "光模块",
            "算力",
            "服务器",
        )
        if any(k in blob for k in analyst_terms) and any(k in blob for k in tech_subject_terms):
            return False

    # 第一段：标签豁免权（垂直路由或上游已打板块标签时直接放行）
    if vertical_target_sector and str(vertical_target_sector).strip() == sec:
        return True
    if item_tags and sec in {str(x).strip() for x in item_tags if str(x).strip()}:
        return True

    # 第二段：关键词强匹配（用于宽池条目）
    if sec == "银行":
        bank_terms = (
            "银行",
            "汇丰",
            "摩根大通",
            "摩根士丹利",
            "高盛",
            "花旗",
            "净息差",
            "不良率",
            "拨备",
            "存款",
            "贷款",
            "息差",
        )
        if any(k in blob for k in _BANK_FALSE_POSITIVE_TERMS):
            return False
        return any(k in blob for k in bank_terms)
    if sec == "黄金":
        if any(k in blob for k in _GOLD_FALSE_POSITIVE_TERMS):
            return any(k in blob for k in ("金价", "现货黄金", "COMEX", "贵金属", "央行购金", "黄金储备", "黄金ETF", "沪金", "纽约期金"))
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
_ROUTER_MAX_IDS_PER_SECTOR = 3
_ROUTER_EMPTY_INSIGHT = "本轮未筛出高置信度板块资讯，暂不强行归因。"
_ROUTER_FILLED_FALLBACK_INSIGHT = "已筛出高置信度线索，详见下方事件与影响。"
_ROUTER_CANDIDATES_PER_SECTOR = 8
_SECTOR_REWRITE_MAX_ITEMS = 5
_SECTOR_REWRITE_VAGUE_TERMS: tuple[str, ...] = (
    "可能",
    "大概",
    "或许",
    "似乎",
    "或将",
    "有望",
    "预计",
    "有机会",
    "值得关注",
    "需关注",
    "需观察",
    "有待",
    "不排除",
)
_ROUTER_SYSTEM_PROMPT = (
    "你是金融资讯Router。菜单已按 [科技, 新能源, 港股, 黄金, 有色, 银行] 分组。"
    "只在各板块自己的候选中选 ID。\n"
    "规则：宁缺毋滥；无强相关就 items=[]；不要为了凑数塞宏观/海外/泛财经。"
    "同一事件默认只归一个主板块，强联动最多两个板块，严禁三板块重复。"
    "每板块最多3个ID，insight不超过30字。\n"
    "只返回严格JSON，六个中文键必须齐全。格式："
    '{"科技":{"insight":"算力链延续强势","items":[1,3]},"新能源":{"insight":"暂无超预期产业事件","items":[]}}'
)


def _router_enabled() -> bool:
    raw = os.environ.get("FINANCE_LLM_ROUTER_ENABLED", "").strip()
    if not raw:
        # 默认关闭，避免纯信源拉取被 LLM router 阻塞 20-30s。
        return False
    return raw in ("1", "true", "TRUE", "yes", "YES")


def _router_menu_max_items() -> int:
    raw = os.environ.get("FINANCE_LLM_ROUTER_MENU_MAX_ITEMS", "").strip()
    try:
        v = int(raw) if raw else 30
    except ValueError:
        v = 30
    return max(12, min(48, v))


def _router_timeout_sec() -> int:
    raw = os.environ.get("FINANCE_LLM_ROUTER_TIMEOUT_SEC", "").strip()
    try:
        # 默认 10s 保底，超时快速回退到 fallback_grouped。
        v = int(raw) if raw else 10
    except ValueError:
        v = 10
    return max(6, min(20, v))


def _sector_rewrite_enabled() -> bool:
    raw = os.environ.get("FINANCE_SECTOR_LLM_REWRITE_ENABLED", "").strip()
    if not raw:
        return False
    return raw in ("1", "true", "TRUE", "yes", "YES")


def _sector_rewrite_timeout_sec() -> int:
    raw = os.environ.get("FINANCE_SECTOR_LLM_REWRITE_TIMEOUT_SEC", "").strip()
    try:
        v = int(raw) if raw else 8
    except ValueError:
        v = 8
    return max(3, min(20, v))


def _sector_rewrite_load_config() -> tuple[str, str, str]:
    base = os.environ.get("FINANCE_SECTOR_LLM_BASE_URL", "").strip()
    key = os.environ.get("FINANCE_SECTOR_LLM_API_KEY", "").strip()
    model = os.environ.get("FINANCE_SECTOR_LLM_MODEL", "").strip()
    if base and key and model:
        return base.rstrip("/"), key, model
    return _router_load_config()


def _router_compact_retry_enabled() -> bool:
    raw = os.environ.get("FINANCE_LLM_ROUTER_COMPACT_RETRY", "").strip()
    if not raw:
        return True
    return raw in ("1", "true", "TRUE", "yes", "YES")


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


def _router_parse_result(raw: str) -> dict[str, Any]:
    """解析新嵌套格式的 Router JSON，返回 {ids_by_sec, insight_by_sec, reason_by_id}。
    兼容旧平铺列表格式（compact retry 等降级情况）。"""
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
    ids_by_sec: dict[str, list[int]] = {sec: [] for sec in _ROUTER_SECTORS}
    insight_by_sec: dict[str, str] = {}
    reason_by_id: dict[int, str] = {}
    for sec in _ROUTER_SECTORS:
        val = obj.get(sec)
        if val is None:
            continue
        if isinstance(val, dict):
            # 新嵌套格式 {"insight": str, "items": [int, ...]}；兼容旧 {"id": int, "reason": str}
            insight = str(val.get("insight") or "").strip()
            insight_by_sec[sec] = insight or _ROUTER_EMPTY_INSIGHT
            arr: list[int] = []
            for entry in val.get("items") or []:
                if isinstance(entry, int):
                    arr.append(entry)
                    continue
                if isinstance(entry, str) and entry.strip().isdigit():
                    arr.append(int(entry.strip()))
                    continue
                if not isinstance(entry, dict):
                    continue
                idx_raw = entry.get("id")
                reason = str(entry.get("reason") or "").strip()
                if isinstance(idx_raw, int):
                    arr.append(idx_raw)
                    if reason:
                        reason_by_id[idx_raw] = reason
                elif isinstance(idx_raw, str) and idx_raw.strip().isdigit():
                    ii = int(idx_raw.strip())
                    arr.append(ii)
                    if reason:
                        reason_by_id[ii] = reason
            ids_by_sec[sec] = arr
        elif isinstance(val, list):
            # 旧平铺格式兼容 [int, ...]
            arr2: list[int] = []
            for x in val:
                if isinstance(x, int):
                    arr2.append(x)
                elif isinstance(x, str) and x.strip().isdigit():
                    arr2.append(int(x.strip()))
            ids_by_sec[sec] = arr2
    return {
        "ids_by_sec": ids_by_sec,
        "insight_by_sec": insight_by_sec,
        "reason_by_id": reason_by_id,
    }


def _router_build_candidates(
    by_sec: dict[str, list[dict[str, Any]]],
    gm_items: list[dict[str, Any]],
    deep_items: list[dict[str, Any]],
    other_flash_items: list[dict[str, Any]],
    macro_items_raw: list[dict[str, Any]],
    *,
    max_items: int = 40,
) -> list[dict[str, Any]]:
    """构建 Router 分组菜单：每板块独立配额，避免高频板块挤压低频板块。"""
    per_sector_cap = min(_ROUTER_CANDIDATES_PER_SECTOR, max(4, max_items // max(1, len(_ROUTER_SECTORS))))
    candidates: list[dict[str, Any]] = []

    def wrap(it: dict[str, Any], sec: str, source_type: str, default_source: str = "") -> dict[str, Any]:
        title = _clean_display_text(str(it.get("title") or "")).strip()
        body = _clean_display_text(str(it.get("clean_text") or it.get("summary") or "")).strip()
        menu_summary = body[:80] + ("..." if len(body) > 80 else "")
        raw = _annotate_item_for_sector(it, sec, line_source=source_type)
        return {
            "title": title or body[:120],
            "summary": menu_summary,
            "clean_text": str(it.get("clean_text") or it.get("summary") or title),
            "source_name": str(it.get("source_name") or default_source or _item_source_label(it)),
            "published_at": str(it.get("published_at") or ""),
            "candidate_sector": sec,
            "candidate_source_type": source_type,
            "raw_item": raw,
        }

    def item_key(it: dict[str, Any]) -> str:
        title = _clean_display_text(str(it.get("title") or "")).strip()
        body = _clean_display_text(str(it.get("clean_text") or it.get("summary") or "")).strip()
        return _title_dedup_key(title or body[:80])

    def matches_sector(sec: str, it: dict[str, Any]) -> bool:
        title = str(it.get("title") or "")
        body = str(it.get("clean_text") or it.get("summary") or "")
        if not _sector_item_is_usable(sec, it):
            return False
        if _router_source_requires_strict_match(it):
            return _router_keyword_strong_match(sec, title, body)
        vts = str(it.get("vertical_target_sector") or "").strip()
        tags = {str(x).strip() for x in (it.get("sector_tags") or []) if str(x).strip()}
        if vts == sec or sec in tags:
            return True
        txt = f"{title} {body}"
        if sec in {"银行", "黄金", "有色"}:
            return _router_keyword_strong_match(sec, title, body)
        return sec in sectors_for_text(txt)

    def sort_key(sec: str, it: dict[str, Any]) -> tuple[int, int, datetime]:
        return (
            _sec_deep_whitelist_rank(sec, it),
            _depth_source_rank(it),
            _parse_published_at(str(it.get("published_at") or "")) or datetime.min.replace(tzinfo=timezone(timedelta(hours=8))),
        )

    macro_synth_items: list[dict[str, Any]] = []
    for it in macro_items_raw[:12]:
        if isinstance(it, dict):
            macro_synth_items.append(
                {
                    "title": str(it.get("title") or ""),
                    "clean_text": str(it.get("detail") or it.get("title") or ""),
                    "published_at": str(it.get("published_at") or ""),
                    "source_name": "百度热榜",
                    "sector_line_source": "macro_hot",
                }
            )

    for sec in _ROUTER_SECTORS:
        seen: set[str] = set()

        deep_pool = [it for it in deep_items if isinstance(it, dict) and matches_sector(sec, it)]
        flash_pool = [it for it in (by_sec.get(sec) or []) if isinstance(it, dict)]
        broad_pool = [
            it
            for it in [*gm_items[:28], *other_flash_items[:30], *macro_synth_items]
            if isinstance(it, dict) and matches_sector(sec, it)
        ]

        deep_pool.sort(key=lambda x: sort_key(sec, x), reverse=True)
        flash_pool.sort(
            key=lambda x: _parse_published_at(str(x.get("published_at") or "")) or datetime.min.replace(tzinfo=timezone(timedelta(hours=8))),
            reverse=True,
        )
        broad_pool.sort(key=lambda x: sort_key(sec, x), reverse=True)

        picked: list[dict[str, Any]] = []

        def add_from(pool: list[dict[str, Any]], source_type: str, limit: int) -> int:
            added = 0
            for it in pool:
                if len(picked) >= per_sector_cap or added >= limit:
                    break
                k = item_key(it)
                if not k or k in seen:
                    continue
                seen.add(k)
                picked.append(wrap(it, sec, source_type))
                added += 1
            return added

        deep_n = add_from(deep_pool, "deep", 6)
        flash_limit = 2
        add_from(flash_pool, "flash", flash_limit)
        add_from(broad_pool, "broad", 1)
        candidates.extend(picked[:per_sector_cap])

    return candidates


def _router_build_menu(candidates: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    type_label = {"deep": "深度", "flash": "快讯", "broad": "宽池"}
    for sec in _ROUTER_SECTORS:
        sec_rows = [(idx, it) for idx, it in enumerate(candidates) if str(it.get("candidate_sector") or "") == sec]
        lines.append(f"【{sec}候选】")
        if not sec_rows:
            lines.append("（无候选）")
            continue
        for idx, it in sec_rows:
            src = str(it.get("source_name") or "未知来源").strip()
            st = type_label.get(str(it.get("candidate_source_type") or ""), "候选")
            title = _clean_display_text(str(it.get("title") or "")).strip()
            summ = _clean_display_text(str(it.get("summary") or "")).strip()[:80]
            if len(summ) >= 80:
                summ = summ.rstrip(".。") + "..."
            lines.append(f"[ID: {idx}] ({st}) {src} - {title} - {summ}")
    return "\n".join(lines)


def _router_compact_grouped_candidates(candidates: list[dict[str, Any]], *, per_sector: int = 3) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for sec in _ROUTER_SECTORS:
        n = 0
        for it in candidates:
            if str(it.get("candidate_sector") or "") != sec:
                continue
            compact.append(it)
            n += 1
            if n >= per_sector:
                break
    return compact


def _router_candidate_diagnostics(candidates: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    diag: dict[str, dict[str, int]] = {}
    for sec in _ROUTER_SECTORS:
        rows = [it for it in candidates if str(it.get("candidate_sector") or "") == sec]
        diag[sec] = {
            "candidate_count": len(rows),
            "deep_candidate_count": sum(1 for it in rows if str(it.get("candidate_source_type") or "") == "deep"),
            "flash_candidate_count": sum(1 for it in rows if str(it.get("candidate_source_type") or "") == "flash"),
            "broad_candidate_count": sum(1 for it in rows if str(it.get("candidate_source_type") or "") == "broad"),
        }
    return diag


def _router_call_llm(menu_text: str, *, timeout_sec: int = 10) -> dict[str, Any]:
    base, key, model = _router_load_config()
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _ROUTER_SYSTEM_PROMPT},
            {"role": "user", "content": menu_text},
        ],
        "temperature": 0.1,
        "max_tokens": 600,
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(
        f"{base}/chat/completions",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urlrequest.urlopen(req, timeout=max(1, timeout_sec), context=ssl.create_default_context()) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib_error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"router HTTP {e.code}: {detail[:500]}") from e
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"router 请求失败: {e!s}") from e
    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError("router 响应无 choices")
    content = str(((choices[0] or {}).get("message") or {}).get("content") or "")
    if not content.strip():
        raise RuntimeError("router content 为空")
    return _router_parse_result(content)


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
    t_start = time.perf_counter()
    t_marks: dict[str, float] = {}
    candidates = _router_build_candidates(
        by_sec,
        gm_items,
        deep_items,
        other_flash_items,
        macro_items_raw,
        max_items=_router_menu_max_items(),
    )
    t_marks["build_candidates"] = round(time.perf_counter() - t_start, 3)
    if not candidates:
        return legacy, {"status": "no_candidates", "router_timing": t_marks}
    menu_text = _router_build_menu(candidates)
    t_marks["build_menu"] = round(time.perf_counter() - t_start - t_marks["build_candidates"], 3)
    candidate_diag = _router_candidate_diagnostics(candidates)
    retry_mode = "none"

    def assemble_from_ids(
        ids_by_sec: dict[str, list[int]],
        reason_by_id: dict[int, str],
        *,
        fallback_reason: str = "",
        require_strong: bool = False,
    ) -> dict[str, list[dict[str, Any]]]:
        items_by_sec: dict[str, list[dict[str, Any]]] = {sec: [] for sec in _ROUTER_SECTORS}
        for sec in _ROUTER_SECTORS:
            seen_idx: set[int] = set()
            n_sec = 0
            for idx in ids_by_sec.get(sec) or []:
                if n_sec >= _ROUTER_MAX_IDS_PER_SECTOR:
                    break
                if not isinstance(idx, int):
                    continue
                if idx < 0 or idx >= len(candidates) or idx in seen_idx:
                    continue
                cand = candidates[idx]
                if str(cand.get("candidate_sector") or "") != sec:
                    continue
                if require_strong and not _router_keyword_strong_match(
                    sec,
                    str(cand.get("title") or ""),
                    str(cand.get("clean_text") or cand.get("summary") or ""),
                ):
                    continue
                seen_idx.add(idx)
                base_item = dict(cand.get("raw_item") or {})
                if not base_item:
                    continue
                if not base_item.get("clean_text"):
                    base_item["clean_text"] = cand.get("clean_text") or cand.get("summary") or base_item.get("title") or ""
                tags = base_item.get("sector_tags") or []
                tags_clean = [str(x).strip() for x in tags if str(x).strip()]
                if sec not in tags_clean:
                    tags_clean.insert(0, sec)
                base_item["sector_tags"] = tags_clean
                base_item["vertical_target_sector"] = sec
                base_item["candidate_sector"] = sec
                base_item["candidate_source_type"] = cand.get("candidate_source_type") or ""
                if not str(base_item.get("sector_line_source") or "").strip():
                    base_item["sector_line_source"] = "llm_router"
                base_item["llm_reason"] = reason_by_id.get(idx, "") or fallback_reason
                items_by_sec[sec].append(_annotate_item_for_sector(base_item, sec, line_source="llm_router"))
                n_sec += 1
        return _dedupe_router_items_across_sectors(items_by_sec)

    def fallback_grouped(reason: str) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
        ids_by_sec: dict[str, list[int]] = {sec: [] for sec in _ROUTER_SECTORS}
        for idx, cand in enumerate(candidates):
            sec = str(cand.get("candidate_sector") or "")
            if (
                sec in ids_by_sec
                and len(ids_by_sec[sec]) < _ROUTER_MAX_IDS_PER_SECTOR
                and _router_keyword_strong_match(sec, str(cand.get("title") or ""), str(cand.get("clean_text") or cand.get("summary") or ""))
            ):
                ids_by_sec[sec].append(idx)
        items_by_sec = assemble_from_ids(ids_by_sec, {}, require_strong=True)
        insights = {
            sec: (_ROUTER_FILLED_FALLBACK_INSIGHT if items_by_sec.get(sec) else _ROUTER_EMPTY_INSIGHT)
            for sec in _ROUTER_SECTORS
        }
        return items_by_sec, {
            "status": "fallback_grouped",
            "reason": reason[:300],
            "selected_count": sum(len(v) for v in items_by_sec.values()),
            "menu_count": len(candidates),
            "menu_preview": menu_text[:2000],
            "retry_mode": retry_mode,
            "insights_by_sector": insights,
            "candidate_diagnostics": candidate_diag,
            "router_timing": {
                **t_marks,
                "total": round(time.perf_counter() - t_start, 3),
            },
        }

    llm_t0 = time.perf_counter()
    try:
        routed = _router_call_llm(menu_text, timeout_sec=_router_timeout_sec())
        t_marks["llm_inference"] = round(time.perf_counter() - llm_t0, 3)
    except Exception as exc:  # noqa: BLE001
        t_marks["llm_inference"] = round(time.perf_counter() - llm_t0, 3)
        msg = str(exc).lower()
        is_timeout = ("timed out" in msg) or ("timeout" in msg)
        errors.append(
            {
                "source": "llm_router",
                "stage": "dispatch",
                "code": "LLM_ROUTER_TIMEOUT" if is_timeout else "LLM_ROUTER_FAILED",
                "message": str(exc)[:500],
            }
        )
        return fallback_grouped(str(exc))
    try:
        ids_by_sec = routed.get("ids_by_sec") or {}
        insight_by_sec: dict[str, str] = routed.get("insight_by_sec") or {}
        reason_by_id: dict[int, str] = routed.get("reason_by_id") or {}
        items_by_sec = assemble_from_ids(ids_by_sec, reason_by_id, require_strong=True)
        for sec in _ROUTER_SECTORS:
            if not str(insight_by_sec.get(sec) or "").strip() or (items_by_sec.get(sec) and insight_by_sec.get(sec) == _ROUTER_EMPTY_INSIGHT):
                insight_by_sec[sec] = _ROUTER_FILLED_FALLBACK_INSIGHT if items_by_sec.get(sec) else _ROUTER_EMPTY_INSIGHT
        return items_by_sec, {
            "status": "ok_compact_retry" if retry_mode == "compact_retry" else "ok",
            "selected_count": sum(len(v) for v in items_by_sec.values()),
            "menu_count": len(candidates),
            "menu_preview": menu_text[:2000],
            "retry_mode": retry_mode,
            "insights_by_sector": insight_by_sec,
            "candidate_diagnostics": candidate_diag,
            "router_timing": {
                **t_marks,
                "total": round(time.perf_counter() - t_start, 3),
            },
        }
    except Exception as exc:  # noqa: BLE001
        errors.append(
            {
                "source": "llm_router",
                "stage": "assemble",
                "code": "LLM_ROUTER_FAILED",
                "message": str(exc)[:500],
            }
        )
        return fallback_grouped(str(exc))


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


def _sector_event_summary(sec: str, it: dict[str, Any], title: str) -> str:
    body = _clean_display_text(str(it.get("clean_text") or it.get("summary") or it.get("detail") or ""))
    if body and title and body.startswith(title[: min(12, len(title))]):
        body = body[len(title) :].lstrip(" ，,。:-—")
    if body and (_is_collection_title(title) or len(body) > 180):
        focused = _sector_focused_sentence(sec, body, max_len=150)
        if focused:
            return focused
    if body:
        sentences = re.split(r"[。！？；;\n]\s*", body)
        for sent in sentences:
            clean = _clean_display_text(sent)
            if len(clean) >= 12 and not any(k in clean for k in _VAGUE_DISPLAY_TERMS):
                return _clip_flash_text(clean, max_len=150)
        return _clip_flash_text(body, max_len=150)
    if title:
        return _clip_flash_text(title, max_len=120)
    return "本条暂无可用正文摘要，请结合来源复核。"


def _sector_subtheme(sec: str, title: str, body: str) -> str:
    blob = f"{title} {body}"
    if sec == "科技":
        if "机器人" in blob or "具身智能" in blob:
            return "robotics"
        if any(k in blob for k in ("融资", "IPO", "红筹", "港股IPO", "估值")) and any(k in blob for k in ("大模型", "AI", "人工智能")):
            return "ai_capital"
        if any(k in blob for k in ("OpenAI", "英伟达", "GPU", "AI芯片", "算力", "数据中心", "服务器", "CPO", "光模块")):
            return "compute"
        if any(k in blob for k in ("AI社交", "AI应用", "卖货", "商业化", "录音笔", "会议", "多闪")):
            return "ai_application"
        if any(k in blob for k in ("AI幻觉", "system prompt", "大模型")):
            return "model_quality"
    if sec == "新能源":
        if "电池安全" in blob:
            return "ev_battery"
        if any(k in blob for k in ("碳酸锂", "锂价", "锂矿", "电池材料", "正极材料", "负极材料", "锂电材料")):
            return "lithium_cost"
        if any(k in blob for k in ("电力", "电网", "储能", "绿电", "固态变压器", "铜会成为新石油")):
            return "power_infra"
        if any(k in blob for k in ("电池", "新能源车", "电动车", "汽车")):
            return "ev_battery"
    if sec == "黄金":
        if any(k in blob for k in ("央行", "购金", "增持", "黄金储备")):
            return "central_bank_gold"
        if any(k in blob for k in ("战争", "冲突", "伊朗", "中东", "避险")):
            return "safe_haven"
    if sec == "有色":
        if any(k in blob for k in ("铜", "伦铜", "沪铜")):
            return "copper"
        if any(k in blob for k in ("锂", "碳酸锂", "锂矿")):
            return "lithium"
        if any(k in blob for k in ("铝", "氧化铝", "电解铝")):
            return "aluminum"
        if any(k in blob for k in ("镍", "镍价")):
            return "nickel"
    if sec == "港股":
        if any(k in blob for k in ("南向", "北水", "港股通")):
            return "southbound"
        if any(k in blob for k in ("SaaS", "软件", "云服务", "企业服务")):
            return "hk_software"
        if any(k in blob for k in ("IPO", "H股", "招股", "上市", "红筹")):
            return "hk_ipo"
    return "general"


def _display_title_for_sector(sec: str, title: str, body: str) -> str:
    if not _is_collection_title(title):
        return title
    anchors = _sector_focus_terms(sec)
    candidates = re.split(r"[。！？；;]\s*", body)
    for sent in candidates:
        clean = _clean_display_text(sent)
        if len(clean) < 12:
            continue
        if any(a in clean for a in anchors):
            return _clip_flash_text(clean, max_len=46)
    return title


def _sector_impact_summary(sec: str, it: dict[str, Any], title: str, body: str) -> str:
    anchors = _sector_anchor_hits(sec, title, body)
    anchor_text = "、".join(anchors[:4]) if anchors else sec
    subtheme = _sector_subtheme(sec, title, body)
    if sec == "科技":
        if subtheme == "robotics":
            return f"科技主线锚点：{anchor_text}；机器人是盘面交易抓手，重点看减速器、执行器、设备链和资金持续性。"
        if subtheme == "ai_capital":
            return f"科技主线锚点：{anchor_text}；大模型融资/港股 IPO 预期强化 AI 资产证券化与产业资本入场叙事。"
        if subtheme == "compute":
            return f"科技主线锚点：{anchor_text}；关注 GPU、AI 芯片、数据中心和国产替代链条的供需变化。"
        if subtheme == "ai_application":
            return f"科技主线锚点：{anchor_text}；关注 AI 应用商业化、用户增长与大厂生态合作能否转成业绩线索。"
        if subtheme == "model_quality":
            return f"科技主线锚点：{anchor_text}；模型能力与可靠性议题升温，适合观察 AI 应用落地门槛。"
        return f"科技主线锚点：{anchor_text}；优先关注算力、芯片、AI 应用或数据中心链条的资金映射。"
    if sec == "新能源":
        if subtheme == "power_infra":
            return f"新能源联动锚点：{anchor_text}；算力扩张抬升电力基础设施重要性，关注储能、电网设备和绿电消纳。"
        if subtheme == "lithium_cost":
            return f"新能源联动锚点：{anchor_text}；锂价和电池材料价格影响电芯成本、锂电链利润分配与上游资源品弹性。"
        if subtheme == "ev_battery":
            return f"新能源联动锚点：{anchor_text}；电池安全与新能源车竞争影响产业链信任度和后续监管/标准预期。"
        return f"新能源联动锚点：{anchor_text}；重点看电力设备、储能、绿电消纳或新能源车链条是否受益。"
    if sec == "黄金":
        return f"黄金锚点：{anchor_text}；关注避险情绪、央行购金、美元/美债利率与金价弹性的传导。"
    if sec == "有色":
        if subtheme == "copper":
            return f"有色锚点：{anchor_text}；铜价、库存和矿端扰动影响工业金属定价与资源股弹性。"
        if subtheme == "lithium":
            return f"有色锚点：{anchor_text}；锂价变化牵动电池材料成本、锂矿利润和新能源链价格预期。"
        if subtheme == "aluminum":
            return f"有色锚点：{anchor_text}；铝价、氧化铝和电解铝供给约束影响产业链利润分配。"
        if subtheme == "nickel":
            return f"有色锚点：{anchor_text}；镍价和供给扰动影响不锈钢及电池材料链条预期。"
        return f"有色锚点：{anchor_text}；关注具体金属品种的价格、库存、供给扰动与需求预期。"
    if sec == "港股":
        if subtheme == "southbound":
            return f"港股锚点：{anchor_text}；南向资金和港股通变化直接影响恒生科技与中资资产风险偏好。"
        if subtheme == "hk_software":
            return f"港股锚点：{anchor_text}；SaaS/软件逆势线索反映港股科技资产的业绩韧性和估值修复可能。"
        if subtheme == "hk_ipo":
            return f"港股锚点：{anchor_text}；IPO/H股/红筹动态强化中资科技资产证券化与港股新经济供给。"
        return f"港股锚点：{anchor_text}；关注恒生科技、南向资金与中资资产风险偏好的变化。"
    if sec == "银行":
        return f"银行锚点：{anchor_text}；关注净息差、信贷投放、同业存款定价与监管政策影响。"
    return f"板块锚点：{anchor_text}。"


def _sector_content_angle(sec: str, it: dict[str, Any], title: str, body: str) -> str:
    subtheme = _sector_subtheme(sec, title, body)
    if sec == "科技":
        if subtheme == "robotics":
            return "可从“机器人行情是不是硬科技新主线”切入，讲资金为何选择设备链和核心零部件。"
        if subtheme == "ai_capital":
            return "可从“大模型公司融资和赴港 IPO 预期升温”切入，讲 AI 公司如何从技术叙事走向资本化。"
        if subtheme == "compute":
            return "可从“AI 算力链的新变量”切入，追问国产替代、供给瓶颈或产业链受益环节。"
        if subtheme == "ai_application":
            return "可从“AI 应用从讲故事到卖产品”切入，判断用户增长和商业化是否支撑估值。"
        if subtheme == "model_quality":
            return "可从“AI 幻觉和模型可靠性卡住落地”切入，解释为什么应用爆发还需要基础能力突破。"
        return "可从“硬科技主线是否延续”切入，结合盘面强弱筛选可讲的产业链节点。"
    if sec == "新能源":
        if subtheme == "power_infra":
            return "可从“算力扩张背后的电力基础设施”切入，解释储能、电网设备和绿电消纳机会。"
        if subtheme == "lithium_cost":
            return "可从“锂价如何重定价锂电产业链利润”切入，讲清电池材料成本、锂矿弹性和整车端价格预期。"
        if subtheme == "ev_battery":
            return "可从“电池安全如何影响新能源车信任与监管标准”切入，连接产业链和消费端风险。"
        return "可从“新能源链条景气修复或政策催化”切入，判断是短线情绪还是基本面变化。"
    if sec == "黄金":
        return "可从“避险与央行购金如何影响金价”切入，适合做宏观到资产价格的解释型内容。"
    if sec == "有色":
        if subtheme == "copper":
            return "可从“铜价为何牵动工业金属和资源股”切入，讲库存、矿端扰动与需求预期。"
        if subtheme == "lithium":
            return "可从“锂价变化如何传导到电池材料和上游资源品”切入，区分成本压力与资源股弹性。"
        if subtheme == "aluminum":
            return "可从“铝价和供给约束如何影响有色链条”切入，讲氧化铝、电解铝和需求端验证。"
        if subtheme == "nickel":
            return "可从“镍价扰动如何影响不锈钢和电池材料”切入，判断是供给冲击还是需求修复。"
        return "可从“商品价格与供需扰动如何映射 A 股资源品”切入，重点讲清具体金属品种。"
    if sec == "港股":
        if subtheme == "southbound":
            return "可从“南向资金是否重新定价港股核心资产”切入，连接恒生科技和中资资产风险偏好。"
        if subtheme == "hk_software":
            return "可从“港股 SaaS/软件为何逆势”切入，讲业绩韧性、估值修复和科技资产分化。"
        if subtheme == "hk_ipo":
            return "可从“中资科技资产赴港证券化升温”切入，解释 IPO 供给和港股新经济定价。"
        return "可从“港股风险偏好和南向资金是否回暖”切入，连接恒生科技与中资资产定价。"
    if sec == "银行":
        return "可从“利率、信贷和监管如何影响银行估值”切入，避免把泛宏观新闻误读成银行利好。"
    return "可从事件本身的市场影响切入，补充盘面验证后再开稿。"


def _sector_item_view_model(sec: str, it: dict[str, Any], *, tpart: str, s_prefix: str, src_label: str) -> dict[str, Any] | None:
    title = _clean_display_text(str(it.get("title") or ""))
    body = _clean_display_text(str(it.get("clean_text") or it.get("summary") or it.get("detail") or title))
    if _is_collection_title(title) and len(body) < 40:
        return None
    event = _sector_event_summary(sec, it, title)
    impact = _sector_impact_summary(sec, it, title, body)
    angle = _sector_content_angle(sec, it, title, body)
    display_title = _display_title_for_sector(sec, title, body) or event
    return {
        "raw_item": it,
        "title": title,
        "body": body,
        "display_title": display_title,
        "event": event,
        "impact": impact,
        "angle": angle,
        "tpart": tpart,
        "s_prefix": s_prefix,
        "src_label": src_label,
    }


def _format_sector_view_model_lines(vm: dict[str, Any]) -> list[str]:
    display_title = str(vm.get("display_title") or vm.get("event") or "")
    event = str(vm.get("event") or "")
    impact = str(vm.get("impact") or "")
    angle = str(vm.get("angle") or "")
    tpart = str(vm.get("tpart") or "")
    s_prefix = str(vm.get("s_prefix") or "")
    src_label = str(vm.get("src_label") or "")
    lines = [f"- 🔹 {s_prefix}{tpart} **{display_title}** （{src_label}）"]
    lines.append(f"  - 事件：{event}")
    lines.append(f"  - 影响：{impact}")
    lines.append(f"  - 角度：{angle}")
    return lines


def _format_sector_item_lines(sec: str, it: dict[str, Any], *, tpart: str, s_prefix: str, src_label: str) -> list[str]:
    vm = _sector_item_view_model(sec, it, tpart=tpart, s_prefix=s_prefix, src_label=src_label)
    return _format_sector_view_model_lines(vm) if vm else []


def _sector_rewrite_text_has_vague_terms(text: str) -> bool:
    return any(term in (text or "") for term in _SECTOR_REWRITE_VAGUE_TERMS)


def _sector_rewrite_build_prompt(sec: str, insight: str, view_models: list[dict[str, Any]]) -> tuple[str, str]:
    items = [
        {
            "index": idx,
            "title": str(vm.get("display_title") or vm.get("title") or ""),
            "source": str(vm.get("src_label") or ""),
            "event": str(vm.get("event") or ""),
            "impact": str(vm.get("impact") or ""),
            "angle": str(vm.get("angle") or ""),
        }
        for idx, vm in enumerate(view_models[:_SECTOR_REWRITE_MAX_ITEMS])
    ]
    system_prompt = (
        "你是金融自媒体板块快照润色器，只负责把已给事实改写得更像研究员口径。\n"
        "Safety Boundary:\n"
        "1. 只能使用输入里的标题、事件、影响、角度和来源信息，不得新增事实、数值、机构名、时间或行情。\n"
        "2. 不得改变数字、涨跌方向、主体关系和板块归因；不确定就沿用原文。\n"
        "3. 不参与选新闻、不删除新闻、不改变 index；items 必须逐条返回。\n"
        "4. insight 只能一句话，事件/影响/角度要短句、自然、可直接发飞书。\n"
        "5. 严禁使用模糊性表达！金融自媒体最忌讳“可能、大概、或许”。润色时把“可能利好”改为更具确定性的逻辑关联，例如“直接利好算力租赁环节”。"
    )
    user_prompt = (
        f"板块：{sec}\n"
        f"规则洞察：{insight}\n"
        "请返回严格 JSON：{\"insight\":\"一句板块洞察\",\"items\":[{\"index\":0,\"event\":\"...\",\"impact\":\"...\",\"angle\":\"...\"}]}\n"
        "输入条目：\n"
        + json.dumps(items, ensure_ascii=False)
    )
    return system_prompt, user_prompt


def _sector_rewrite_parse_json(raw: str) -> dict[str, Any]:
    s = (raw or "").strip()
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", s)
        if not m:
            raise ValueError("sector rewrite 输出无法解析为 JSON object") from None
        obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("sector rewrite 输出根节点必须为 object")
    return obj


def _sector_rewrite_call_llm(
    sec: str,
    view_models: list[dict[str, Any]],
    insight: str,
    *,
    base: str,
    key: str,
    model: str,
    timeout_sec: int,
) -> tuple[str, list[dict[str, Any]]]:
    system_prompt, user_prompt = _sector_rewrite_build_prompt(sec, insight, view_models)
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.15,
        "max_tokens": 900,
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(
        f"{base}/chat/completions",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urlrequest.urlopen(req, timeout=max(1, timeout_sec), context=ssl.create_default_context()) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib_error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"sector rewrite HTTP {e.code}: {detail[:500]}") from e
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"sector rewrite 请求失败: {e!s}") from e
    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError("sector rewrite 响应无 choices")
    content = str(((choices[0] or {}).get("message") or {}).get("content") or "")
    obj = _sector_rewrite_parse_json(content)
    rewritten_insight = _clean_display_text(str(obj.get("insight") or ""))
    if not rewritten_insight or _sector_rewrite_text_has_vague_terms(rewritten_insight):
        raise ValueError("sector rewrite insight 为空或包含模糊性表达")
    items = obj.get("items")
    if not isinstance(items, list) or len(items) != len(view_models):
        raise ValueError("sector rewrite items 数量不匹配")
    by_index: dict[int, dict[str, str]] = {}
    for entry in items:
        if not isinstance(entry, dict):
            raise ValueError("sector rewrite item 必须为 object")
        idx = entry.get("index")
        if not isinstance(idx, int) or idx < 0 or idx >= len(view_models) or idx in by_index:
            raise ValueError("sector rewrite item index 非法")
        rewritten_fields: dict[str, str] = {}
        for field in ("event", "impact", "angle"):
            value = _clean_display_text(str(entry.get(field) or ""))
            if not value or _sector_rewrite_text_has_vague_terms(value):
                raise ValueError(f"sector rewrite {field} 为空或包含模糊性表达")
            rewritten_fields[field] = _clip_flash_text(value, max_len=180)
        by_index[idx] = rewritten_fields
    if set(by_index) != set(range(len(view_models))):
        raise ValueError("sector rewrite item index 未覆盖全部条目")
    out: list[dict[str, Any]] = []
    for idx, vm in enumerate(view_models):
        neo = dict(vm)
        neo.update(by_index[idx])
        out.append(neo)
    return _clip_flash_text(rewritten_insight, max_len=90), out


def _rewrite_sector_view_models_concurrently(
    sector_payloads: dict[str, dict[str, Any]],
    errors: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if not _sector_rewrite_enabled():
        return {}, {"status": "disabled_by_env", "status_by_sector": {}, "timing_by_sector": {}}
    callable_sectors = {
        sec: payload
        for sec, payload in sector_payloads.items()
        if payload.get("view_models")
    }
    if not callable_sectors:
        return {}, {"status": "no_items", "status_by_sector": {}, "timing_by_sector": {}}
    try:
        base, key, model = _sector_rewrite_load_config()
    except Exception as exc:  # noqa: BLE001
        errors.append(
            {
                "source": "sector_llm_rewrite",
                "stage": "config",
                "code": "SECTOR_LLM_REWRITE_CONFIG_MISSING",
                "message": str(exc)[:500],
            }
        )
        return {}, {"status": "config_missing", "status_by_sector": {}, "timing_by_sector": {}}
    timeout_sec = _sector_rewrite_timeout_sec()
    rewrites: dict[str, dict[str, Any]] = {}
    status_by_sector: dict[str, str] = {}
    timing_by_sector: dict[str, float] = {}
    t_all = time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(6, len(callable_sectors))) as executor:
        future_to_sec = {}
        for sec, payload in callable_sectors.items():
            t0 = time.perf_counter()
            future = executor.submit(
                _sector_rewrite_call_llm,
                sec,
                list(payload.get("view_models") or []),
                str(payload.get("insight") or ""),
                base=base,
                key=key,
                model=model,
                timeout_sec=timeout_sec,
            )
            future_to_sec[future] = (sec, t0)
        for future in as_completed(future_to_sec):
            sec, t0 = future_to_sec[future]
            timing_by_sector[sec] = round(time.perf_counter() - t0, 3)
            try:
                insight, vms = future.result()
            except Exception as exc:  # noqa: BLE001
                status_by_sector[sec] = "fallback_rule"
                errors.append(
                    {
                        "source": "sector_llm_rewrite",
                        "stage": sec,
                        "code": "SECTOR_LLM_REWRITE_FAILED",
                        "message": str(exc)[:500],
                    }
                )
                continue
            status_by_sector[sec] = "ok"
            rewrites[sec] = {"insight": insight, "view_models": vms}
    for sec in callable_sectors:
        status_by_sector.setdefault(sec, "fallback_rule")
    overall = "ok" if rewrites and len(rewrites) == len(callable_sectors) else ("partial_fallback" if rewrites else "fallback_rule")
    return rewrites, {
        "status": overall,
        "enabled": True,
        "timeout_sec": timeout_sec,
        "model": model,
        "status_by_sector": status_by_sector,
        "timing_by_sector": timing_by_sector,
        "total": round(time.perf_counter() - t_all, 3),
    }


def _sector_sentiment_label(sec: str, title: str, body: str, it: dict[str, Any]) -> tuple[str, str]:
    blob = f"{title} {body}"
    subtheme = _sector_subtheme(sec, title, body)
    if sec == "黄金":
        if any(k in blob for k in ("金价下跌", "金价走低", "现货黄金跌", "黄金跌", "贵金属下跌")):
            return _s_emoji("利空"), "利空"
        if subtheme in {"central_bank_gold", "safe_haven"} or any(k in blob for k in ("避险", "央行购金", "央行增持黄金", "地缘冲突")):
            return _s_emoji("利好"), "利好"
    if sec == "新能源" and subtheme == "lithium_cost":
        if _has_negative_finance_terms(blob):
            return _s_emoji("利空"), "利空"
        if any(k in blob for k in ("锂价下跌", "碳酸锂下跌", "价格下行", "价格回落", "供给过剩")):
            return _s_emoji("利空"), "利空"
        if any(k in blob for k in ("锂价上涨", "碳酸锂上涨", "价格反弹", "涨价", "供给收缩")):
            return _s_emoji("利好"), "利好"
        return _s_emoji("中性"), "中性"
    if sec == "新能源" and _has_negative_finance_terms(blob):
        return _s_emoji("利空"), "利空"
    if sec == "有色":
        if any(k in blob for k in ("铜价上涨", "铝价上涨", "锂价上涨", "镍价上涨", "价格反弹", "涨价", "库存下降", "供给收缩")):
            return _s_emoji("利好"), "利好"
        if any(k in blob for k in ("铜价下跌", "铝价下跌", "锂价下跌", "镍价下跌", "价格回落", "库存上升", "供给过剩")):
            return _s_emoji("利空"), "利空"
    s_hint = str(it.get("sentiment_hint") or "").strip()
    s_emoji = str(it.get("sentiment_emoji") or "").strip()
    if not s_hint:
        s_hint = classify_sentiment(blob)
        s_emoji = _s_emoji(s_hint)
    elif not s_emoji:
        s_emoji = _s_emoji(s_hint)
    return s_emoji, s_hint


def _rule_sector_insight(sec: str, items: list[dict[str, Any]]) -> str:
    if not items:
        return _ROUTER_EMPTY_INSIGHT
    themes = [_sector_subtheme(sec, str(it.get("title") or ""), str(it.get("clean_text") or it.get("summary") or "")) for it in items]
    titles_blob = " ".join(str(it.get("title") or "") for it in items)
    if sec == "科技":
        bits: list[str] = []
        if "robotics" in themes:
            bits.append("机器人盘面活跃")
        if "ai_capital" in themes:
            bits.append("大模型融资/港股 IPO 预期升温")
        if "compute" in themes:
            bits.append("AI 算力链仍是硬科技抓手")
        if "ai_application" in themes:
            bits.append("AI 应用商业化线索增多")
        if "model_quality" in themes:
            bits.append("模型可靠性议题升温")
        return "；".join(bits[:3]) + "。" if bits else "科技线索集中在 AI 与硬科技方向，需结合盘面确认主线。"
    if sec == "新能源":
        if "power_infra" in themes:
            return "算力扩张把电力、储能和电网设备推到新能源叙事前台。"
        if "lithium_cost" in themes:
            return "锂价和电池材料成本是新能源链条的核心变量，关注上游资源品弹性与中游利润压力。"
        if "ev_battery" in themes:
            return "新能源车链条关注点转向电池安全、标准与消费者信任。"
    if sec == "黄金":
        if "central_bank_gold" in themes and "safe_haven" in themes:
            return "央行购金与地缘避险共振，黄金线索同时具备宏观和事件催化。"
        if "central_bank_gold" in themes:
            return "央行购金仍是黄金叙事核心，关注金价弹性和储备需求。"
        if "safe_haven" in themes:
            return "地缘冲突抬升避险需求，黄金适合做宏观风险解释线。"
    if sec == "有色":
        if "copper" in themes:
            return "有色线索聚焦铜价、库存与矿端扰动，适合观察资源品映射。"
        if "lithium" in themes:
            return "有色线索聚焦锂价与新能源链条需求预期。"
        if "aluminum" in themes:
            return "有色线索聚焦铝价与供给约束。"
        if "nickel" in themes:
            return "有色线索聚焦镍价与供给扰动，需区分不锈钢和电池材料两条需求线。"
    if sec == "港股":
        if "southbound" in themes or any(k in titles_blob for k in ("南向", "北水")):
            return "港股关注南向资金与恒生科技风险偏好，资金面是主要观察点。"
        if "hk_software" in themes:
            return "港股 SaaS/软件线索强调科技资产分化，逆势品种需看业绩韧性和估值修复。"
        if "hk_ipo" in themes or any(k in titles_blob for k in ("IPO", "H股", "招股")):
            return "港股线索集中在新股/IPO 与中资科技资产证券化。"
    if sec == "银行":
        return "银行线索应聚焦信贷、息差、同业存款和监管定价变化，避免泛央行新闻误读。"
    return "本板块已有可用线索，建议结合盘面强弱再判断开稿优先级。"


def _backfill_hotspots_to_sectors(
    enriched_by_sec: dict[str, list[dict[str, Any]]],
    buckets: list[list[dict[str, Any]]],
    *,
    max_per_sector: int = 6,
) -> dict[str, list[dict[str, Any]]]:
    """将高价值热点反哺六大板块，防止 OpenAI 芯片等只留在今日热点区。"""
    out = {sec: list(enriched_by_sec.get(sec) or []) for sec in SECTOR_ORDER}
    seen: dict[str, set[str]] = {
        sec: {_title_dedup_key(str(x.get("title") or "")) for x in items if isinstance(x, dict)}
        for sec, items in out.items()
    }
    for bucket in buckets:
        for it in bucket:
            if not isinstance(it, dict):
                continue
            sectors = _candidate_sectors_for_item(it, max_sectors=2)
            if not sectors:
                continue
            key = _title_dedup_key(str(it.get("title") or ""))
            if not key:
                continue
            for sec in sectors:
                if len(out.get(sec) or []) >= max_per_sector or key in seen.setdefault(sec, set()):
                    continue
                annotated = _annotate_item_for_sector(it, sec, line_source="hotspot_backfill")
                annotated["hotspot_backfilled"] = True
                out.setdefault(sec, []).append(annotated)
                seen[sec].add(key)
    return out


def _depth_source_rank(it: dict[str, Any]) -> int:
    """深度来源优先级：深度/产业源 > 其他。"""
    src = str(it.get("source_name") or "")
    line_src = str(it.get("sector_line_source") or "")
    deep_hints = ("36氪", "界面", "金十", "格隆汇", "华尔街见闻", "第一财经")
    if line_src == "deep_news" or any(k in src for k in deep_hints):
        return 2
    return 1


def _sec_deep_whitelist_rank(sec: str, it: dict[str, Any]) -> int:
    """板块定制深度白名单：黄金/有色优先金十、华尔街见闻、界面大宗等来源。"""
    src = str(it.get("source_name") or "")
    txt = f"{it.get('title') or ''} {it.get('clean_text') or it.get('summary') or ''}"
    if sec == "黄金":
        if any(k in src for k in ("金十", "华尔街见闻", "界面", "格隆汇")):
            return 3
        if any(k in txt for k in ("黄金", "金价", "现货黄金", "COMEX", "贵金属", "金饰")):
            return 2
    if sec == "有色":
        if any(k in src for k in ("金十", "华尔街见闻", "界面", "格隆汇")):
            return 3
        if any(k in txt for k in ("有色", "工业金属", "铜", "铝", "锌", "镍", "稀土", "LME")):
            return 2
    return 1


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


_TOP_STORY_DATA_TERMS: tuple[str, ...] = (
    "涨超",
    "跌超",
    "涨停",
    "跌停",
    "%",
    "亿元",
    "亿港元",
    "亿美元",
    "Token",
    "净买入",
    "净流入",
    "中标",
    "融资",
    "IPO",
    "上市",
)

_TOP_STORY_CONFLICT_TERMS: tuple[str, ...] = (
    "但",
    "却",
    "暴跌",
    "暴涨",
    "危机",
    "债务",
    "冻结",
    "诉讼",
    "受挫",
    "逆势",
    "分歧",
    "暗战",
    "冲突",
    "交火",
    "换帅",
)

_TOP_STORY_LOW_VALUE_TERMS: tuple[str, ...] = (
    "白桦树汁",
    "智商税",
    "目标价",
    "评级",
    "早餐",
    "早报",
    "24小时",
    "要闻全览",
)


def _top_story_clean_direction_text(text: str) -> str:
    cleaned = _clean_display_text(text)
    replacements = (
        ("这次可能很不一样", "这次利率路径不一样"),
        ("可能很不一样", "利率路径不一样"),
        ("可能", ""),
        ("大概", ""),
        ("或许", ""),
        ("似乎", ""),
        ("或将", "将"),
        ("有望", ""),
        ("预计", ""),
    )
    for old, new in replacements:
        cleaned = cleaned.replace(old, new)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"[，,：:｜| ]+$", "", cleaned).strip()
    return cleaned


def _top_story_sector_labels(it: dict[str, Any], fallback_sec: str = "") -> list[str]:
    labels: list[str] = []
    for sec in (
        fallback_sec,
        str(it.get("display_sector") or ""),
        str(it.get("primary_sector") or ""),
    ):
        if sec in SECTOR_ORDER and sec not in labels:
            labels.append(sec)
    if labels:
        for sec in (
            str(it.get("candidate_sector") or ""),
            str(it.get("vertical_target_sector") or ""),
        ):
            if sec in SECTOR_ORDER and sec not in labels:
                labels.append(sec)
        for sec in it.get("sector_tags") or []:
            s = str(sec).strip()
            if s in SECTOR_ORDER and s not in labels:
                labels.append(s)
    if not labels and fallback_sec:
        for sec in _candidate_sectors_for_item(it, max_sectors=2):
            if sec not in labels:
                labels.append(sec)
    return labels[:2]


def _top_story_score(it: dict[str, Any], *, sector_hint: str = "", now_dt: datetime | None = None) -> float:
    txt = _item_text(it)
    title = str(it.get("title") or "")
    body = str(it.get("clean_text") or it.get("summary") or "")
    score = 0.0
    sectors = _top_story_sector_labels(it, sector_hint)
    if sectors:
        score += 1.2
    score += min(1.6, 0.35 * len(sectors))
    if any(k in txt for k in _TOP_STORY_DATA_TERMS):
        score += 1.8
    if any(k in txt for k in _TOP_STORY_CONFLICT_TERMS):
        score += 1.5
    if any(k in txt for k in ("AI", "OpenAI", "算力", "芯片", "半导体", "储能", "光伏", "南向", "央行购金", "金价", "信贷")):
        score += 1.2
    if any(k in txt for k in ("政策", "监管", "美联储", "央行", "地缘", "冲突", "加息", "降息")):
        score += 1.0
    if len(body) >= 80:
        score += 0.6
    if len(body) >= 180:
        score += 0.4
    if _depth_source_rank(it) >= 2:
        score += 0.6
    if any(k in title for k in ("为什么", "怎么", "？", "如何")):
        score += 0.5
    if any(k in txt for k in _TOP_STORY_LOW_VALUE_TERMS):
        score -= 1.8
    if "source_kind" in it and it.get("source_kind") == "macro_hot":
        score += 0.4
    dt = _parse_published_at(str(it.get("published_at") or ""))
    ref = now_dt or datetime.now(timezone(timedelta(hours=8)))
    if dt is not None:
        age_h = max(0.0, (ref - dt).total_seconds() / 3600.0)
        if age_h <= 4:
            score += 1.0
        elif age_h <= 12:
            score += 0.6
        elif age_h <= 36:
            score += 0.2
    if len(title.strip()) <= 6:
        score -= 2.0
    if not _is_finance_related(it) and not sectors:
        score -= 2.0
    return score


def _top_story_title_direction(it: dict[str, Any], sectors: list[str]) -> str:
    title = _top_story_clean_direction_text(str(it.get("title") or ""))
    body = _clean_display_text(str(it.get("clean_text") or it.get("summary") or ""))
    blob = f"{title} {body}"
    primary = sectors[0] if sectors else ""
    if primary == "科技":
        if any(k in blob for k in ("OpenAI", "大模型", "MaaS", "算力", "AI", "芯片", "半导体")):
            return f"AI 算力链的新变量：{_clip_flash_text(title, max_len=34)}"
        return f"硬科技主线延续性：{_clip_flash_text(title, max_len=34)}"
    if primary == "新能源":
        if any(k in blob for k in ("储能", "电网", "电力", "光伏")):
            return f"新能源从产业事件切入：{_clip_flash_text(title, max_len=34)}"
        return f"新能源链条景气再定价：{_clip_flash_text(title, max_len=34)}"
    if primary == "港股":
        if any(k in blob for k in ("南向", "港股通", "净买入")):
            return f"南向资金重新定价港股：{_clip_flash_text(title, max_len=34)}"
        return f"港股新经济资产证券化：{_clip_flash_text(title, max_len=34)}"
    if primary == "黄金":
        return f"黄金避险与利率交易：{_clip_flash_text(title, max_len=34)}"
    if primary == "有色":
        return f"资源品价格如何映射产业链：{_clip_flash_text(title, max_len=34)}"
    if primary == "银行":
        return f"信贷与银行估值观察：{_clip_flash_text(title, max_len=34)}"
    return _clip_flash_text(title, max_len=42) or "今日值得开稿线索"


def _top_story_reason(it: dict[str, Any], sectors: list[str]) -> str:
    txt = _item_text(it)
    bits: list[str] = []
    if sectors:
        main = sectors[0]
        related = [s for s in sectors[1:] if s != main]
        if related:
            bits.append(f"主线：{main}；关联：" + " / ".join(related))
        else:
            bits.append(f"主线：{main}")
    if any(k in txt for k in _TOP_STORY_DATA_TERMS):
        bits.append("带有明确数据或资金锚点")
    if any(k in txt for k in _TOP_STORY_CONFLICT_TERMS):
        bits.append("自带冲突/反差，适合短视频讲清楚")
    if any(k in txt for k in ("政策", "监管", "央行", "美联储", "地缘", "冲突")):
        bits.append("具备宏观或政策催化")
    if len(bits) < 2 and _depth_source_rank(it) >= 2:
        bits.append("深度来源提供了可复述背景")
    if not bits:
        bits.append("具备可讲的市场触发点")
    return "；".join(bits[:3]) + "。"


def _top_story_fact_anchor(sec: str, it: dict[str, Any]) -> str:
    title = _clean_display_text(str(it.get("title") or ""))
    event = _sector_event_summary(sec, it, title) if sec else _clean_display_text(str(it.get("clean_text") or it.get("summary") or title))
    src = _item_source_label(it)
    src_part = f"来源：{src}；" if src else ""
    return src_part + _clip_flash_text(event or title, max_len=120)


def _build_top_story_lines(
    sector_payloads: dict[str, dict[str, Any]],
    hotspot_items: list[dict[str, Any]],
    major_event_items: list[dict[str, Any]],
    *,
    fetched_at: str,
    limit: int = 3,
) -> list[str]:
    now_dt = _parse_published_at(fetched_at) or datetime.now(timezone(timedelta(hours=8)))
    candidates: list[tuple[float, datetime, str, dict[str, Any]]] = []
    seen_source: set[int] = set()

    def add_candidate(it: dict[str, Any], sec: str = "") -> None:
        if not isinstance(it, dict):
            return
        obj_id = id(it)
        if obj_id in seen_source:
            return
        seen_source.add(obj_id)
        if _is_wire_brief(it) and not any(k in _item_text(it) for k in _TOP_STORY_DATA_TERMS):
            return
        score = _top_story_score(it, sector_hint=sec, now_dt=now_dt)
        if score < 2.4:
            return
        dt = _parse_published_at(str(it.get("published_at") or "")) or now_dt
        candidates.append((score, dt, sec, it))

    for sec, payload in sector_payloads.items():
        for vm in payload.get("view_models") or []:
            raw = vm.get("raw_item") if isinstance(vm, dict) else None
            if isinstance(raw, dict):
                add_candidate(raw, sec)
    for it in hotspot_items:
        add_candidate(it, "")
    for it in major_event_items:
        add_candidate(it, "")

    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    lines: list[str] = []
    seen_keys: set[str] = set()
    used_primary: set[str] = set()
    rank = 1
    deferred: list[tuple[str, dict[str, Any], list[str]]] = []

    def append_story(it: dict[str, Any], sectors: list[str]) -> None:
        nonlocal rank
        main_sec = sectors[0] if sectors else ""
        lines.append(f"**Top {rank}｜标题方向**：{_top_story_title_direction(it, sectors)}")
        lines.append(f"- 为什么值得写：{_top_story_reason(it, sectors)}")
        lines.append(f"- 事实锚点：{_top_story_fact_anchor(main_sec, it)}")
        if main_sec:
            used_primary.add(main_sec)
        rank += 1

    for _, _, sec_hint, it in candidates:
        title = _clean_display_text(str(it.get("title") or ""))
        key = _title_dedup_key(title)
        if not key or key in seen_keys:
            continue
        sectors = _top_story_sector_labels(it, sec_hint)
        if not sectors and sec_hint:
            sectors = [sec_hint]
        main_sec = sectors[0] if sectors else ""
        if main_sec and main_sec in used_primary and len(used_primary) < min(limit, len(SECTOR_ORDER)):
            deferred.append((key, it, sectors))
            continue
        seen_keys.add(key)
        append_story(it, sectors)
        if rank > limit:
            break
    if rank <= limit:
        for key, it, sectors in deferred:
            if key in seen_keys:
                continue
            seen_keys.add(key)
            append_story(it, sectors)
            if rank > limit:
                break
    if not lines:
        return ["- （本轮未筛出足够清晰的开稿主线；建议先看六大板块异动。）"]
    return lines


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

    # --- 社交情报：情绪量化指标板块 ---
    social_intel = sections.get("social_intelligence") or {}
    agg_metrics = social_intel.get("aggregate_metrics") or {}
    
    social_intel_bits: list[str] = []
    if agg_metrics:
        avg_sent = float(
            agg_metrics.get("headline_sentiment", agg_metrics.get("avg_sentiment", 0)) or 0
        )
        sent_label = agg_metrics.get("sentiment_label", "中性")
        fg_index = agg_metrics.get("fear_greed_index", 50)
        fg_label = agg_metrics.get("fear_greed_label", "中性")
        fg_emoji = agg_metrics.get("fear_greed_emoji", "⬜")
        reversal = agg_metrics.get("reversal_signal") or {}
        
        # 情绪分数可视化（-1到1映射到5档）
        sent_emoji = "🟢" if avg_sent > 0.3 else "🟡" if avg_sent > 0 else "🟠" if avg_sent > -0.3 else "🔴"
        social_intel_bits.append(f"**整体情绪分**：{avg_sent:.2f} ({sent_emoji} {sent_label})")
        social_intel_bits.append(f"**恐惧贪婪指数**：{fg_index:.1f} ({fg_emoji} {fg_label})")
        
        # 反转信号提示
        if reversal.get("signal") != 0:
            rev_dir = reversal.get("direction", "")
            rev_reason = reversal.get("reason", "")
            rev_stren = reversal.get("strength", 0)
            rev_emoji = "⚠️" if rev_dir == "short" else "💡"
            social_intel_bits.append(f"{rev_emoji} **信号提示**：{rev_reason}（强度：{rev_stren:.2f}）")
        
        # 股票情绪汇总（Top 3）
        stock_sents = social_intel.get("stock_sentiments") or {}
        if stock_sents:
            sorted_stocks = sorted(
                stock_sents.items(),
                key=lambda x: abs(x[1].get("avg_sentiment", 0)),
                reverse=True
            )[:3]
            if sorted_stocks:
                stock_lines = []
                for stock, data in sorted_stocks:
                    ss = data.get("avg_sentiment", 0)
                    se = "🟢" if ss > 0.2 else "🔴" if ss < -0.2 else "⚪"
                    stock_lines.append(f"{stock}({se} {ss:.2f})")
                social_intel_bits.append("**个股情绪极值**：" + "、".join(stock_lines))
    
    block_social_intel = ("\n".join(f"- {x}" for x in social_intel_bits)) if social_intel_bits else ""

    by_sec = n.get("items_by_sector") or {}
    other_flash_items = n.get("items_other_flash") or []
    macro_items_raw = macro_sec.get("items") or []
    deep_sec = sections.get("deep_news") or {}
    deep_items_for_sectors: list[dict[str, Any]] = deep_sec.get("items") or []
    gm_sec = sections.get("global_macro") or {}
    gm_items: list[dict[str, Any]] = gm_sec.get("items") or []

    # F3/T3：优先走 LLM Router 的板块重组；失败/不可用时自动回退 legacy 规则聚合
    llm_router_sec = sections.get("llm_router") or {}
    llm_router_ok = str(llm_router_sec.get("status") or "") in {"ok", "ok_compact_retry", "fallback_grouped"}
    llm_items_by_sec = llm_router_sec.get("items_by_sector") or {}
    llm_insights: dict[str, str] = llm_router_sec.get("insights_by_sector") or {}
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
    enriched_by_sec = _backfill_hotspots_to_sectors(
        enriched_by_sec,
        [gm_items, deep_items_for_sectors, list(other_flash_items), list(macro_items_raw)],
        max_per_sector=6,
    )

    fixed_sector_order = ["科技", "新能源", "港股", "黄金", "有色", "银行"]
    sector_render_payloads: dict[str, dict[str, Any]] = {}
    for sec in fixed_sector_order:
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
        # 非 LLM 模式做「深度优先」宽容补位（LLM 模式宁缺毋滥，信任模型选择）
        if not llm_router_ok and len(sec_items) < 2:
            need_n = 2 - len(sec_items)
            _seen_keys = set(_sec_seen.keys())
            recover_pool: list[dict[str, Any]] = []

            def _collect_recover(bucket: list[dict[str, Any]], default_line_source: str) -> None:
                for _it in bucket:
                    if not isinstance(_it, dict):
                        continue
                    _title = _clean_display_text(str(_it.get("title") or ""))
                    _body = _clean_display_text(str(_it.get("clean_text") or _it.get("summary") or _title))
                    if not _title and not _body:
                        continue
                    if not _sector_strong_match(
                        sec,
                        _title,
                        _body,
                        item_tags=_it.get("sector_tags") or [],
                        vertical_target_sector=str(_it.get("vertical_target_sector") or ""),
                    ):
                        continue
                    _k = _title_dedup_key(_title or _body[:80])
                    if not _k or _k in _seen_keys:
                        continue
                    _seen_keys.add(_k)
                    _cand = dict(_it)
                    if not str(_cand.get("sector_line_source") or "").strip():
                        _cand["sector_line_source"] = default_line_source
                    recover_pool.append(_cand)

            _collect_recover(deep_items_for_sectors, "deep_news")
            _collect_recover(gm_items, "global_macro")
            _collect_recover(list(by_sec.get(sec) or []), "tagged_catchup")

            recover_pool.sort(
                key=lambda x: (
                    _sec_deep_whitelist_rank(sec, x),
                    _depth_source_rank(x),
                    _parse_published_at(str(x.get("published_at") or ""))
                    or datetime.min.replace(tzinfo=timezone(timedelta(hours=8))),
                ),
                reverse=True,
            )
            for _it in recover_pool[:need_n]:
                sec_items.append(_it)

        sec_items.sort(
            key=lambda x: (
                _sector_source_rank(x, sec),
                0 if _is_collection_title(_clean_display_text(str(x.get("title") or ""))) else 1,
                _parse_published_at(str(x.get("published_at") or "")) or datetime.min.replace(tzinfo=timezone(timedelta(hours=8))),
            ),
            reverse=True,
        )
        sec_items = sec_items[:4]
        out_n = 0
        displayed_items: list[dict[str, Any]] = []
        view_models: list[dict[str, Any]] = []
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
            if not _sector_strong_match(
                sec,
                title,
                body,
                item_tags=it.get("sector_tags") or [],
                vertical_target_sector=str(it.get("vertical_target_sector") or ""),
            ):
                continue
            # 情绪按展示板块重算，避免同一宏观新闻在黄金/有色等板块被误标。
            s_emoji, s_hint = _sector_sentiment_label(sec, title, body, it)
            s_prefix = f"{s_emoji}{s_hint} " if s_emoji and s_hint else ""
            src_label = _item_source_label(it)
            view_model = _sector_item_view_model(sec, it, tpart=tpart, s_prefix=s_prefix, src_label=src_label)
            if view_model:
                view_models.append(view_model)
                displayed_items.append(it)
                out_n += 1
        raw_insight = str(llm_insights.get(sec) or "").strip()
        generic_insights = {
            "",
            _ROUTER_EMPTY_INSIGHT,
            _ROUTER_FILLED_FALLBACK_INSIGHT,
            "本板块出现若干强相关线索，需结合盘面继续确认主线。",
        }
        insight_text = raw_insight if llm_router_ok and raw_insight not in generic_insights and out_n > 0 else _rule_sector_insight(sec, displayed_items)
        fill_lines: list[str] = []
        if not llm_router_ok and out_n < 3:
            for fill_line in _market_sector_fill_lines(sec, m, min_count=3 - out_n):
                fill_lines.append(fill_line)
                out_n += 1
                if out_n >= 3:
                    break
        sector_render_payloads[sec] = {
            "insight": insight_text,
            "view_models": view_models,
            "displayed_items": displayed_items,
            "fill_lines": fill_lines,
        }

    sector_rewrites, sector_rewrite_meta = _rewrite_sector_view_models_concurrently(sector_render_payloads, errors)
    sections["sector_llm_rewrite"] = sector_rewrite_meta

    sector_lines: list[str] = []
    # 固定六大板块顺序，杜绝空板块合并标题
    for sec in fixed_sector_order:
        payload = sector_render_payloads.get(sec) or {}
        rewrite = sector_rewrites.get(sec) or {}
        insight_text = str(rewrite.get("insight") or payload.get("insight") or _ROUTER_EMPTY_INSIGHT)
        view_models = list(rewrite.get("view_models") or payload.get("view_models") or [])
        if sector_lines:
            sector_lines.append("")
        sector_lines.append(f"**【{sec}】**")
        sector_lines.append(f"🧠 **板块洞察**：{insight_text}")
        sector_lines.append("")
        for vm in view_models:
            sector_lines.extend(_format_sector_view_model_lines(vm))
            sector_lines.append("")
        for fill_line in payload.get("fill_lines") or []:
            sector_lines.append(str(fill_line))

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

    top_story_lines = _build_top_story_lines(
        sector_render_payloads,
        hotspot_items,
        major_event_items_for_dedup,
        fetched_at=fetched_at,
        limit=3,
    )

    error_lines: list[str] = []
    router_status = str((sections.get("llm_router") or {}).get("status") or "")
    if router_status in {"ok", "ok_compact_retry"}:
        router_diag = "💡 数据引擎：已启用板块分组 Router 深度去噪与提纯"
        if router_status == "ok_compact_retry":
            router_diag += "（紧凑菜单重试成功）"
    elif router_status == "fallback_grouped":
        router_diag = "⚠️ 数据引擎：LLM 路由超时/异常，已降级为板块分组规则精选"
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
        "SECTOR_LLM_REWRITE_CONFIG_MISSING": "板块小 LLM 润色已开启但模型网关未配置，当前使用规则文案。",
        "SECTOR_LLM_REWRITE_FAILED": "板块小 LLM 润色失败或输出不合规，当前使用规则文案。",
    }
    error_lines.append(f"**告警（中文说明，最多 6 条）**（{len(errors)}）：")
    error_lines.append(f"- {router_diag}")
    rewrite_meta = sections.get("sector_llm_rewrite") or {}
    rewrite_status = str(rewrite_meta.get("status") or "")
    if rewrite_status == "ok":
        error_lines.append("- ✍️ 板块小 LLM 润色：六板块均已完成润色")
    elif rewrite_status == "partial_fallback":
        error_lines.append("- ✍️ 板块小 LLM 润色：部分板块 LLM失效，当前使用规则文案")
    elif rewrite_status:
        error_lines.append("- ✍️ 板块小 LLM 润色：LLM失效，当前使用规则文案")
    if errors:
        for e in errors[:6]:
            code = str(e.get("code") or "")
            msg = error_cn.get(code) or str(e.get("message") or "接口调用失败")
            error_lines.append(f"- **{code}**：{msg}")

    social_intel_section = f"""
### 【🧠 情绪量化指标】（社交情报分析）
{block_social_intel}""" if block_social_intel else ""

    markdown = f"""## 📊 今日信源全量快照 ({fetched_at})

### 【📈 大盘与情绪】
{block_market}{social_intel_section}

### 【🎯 核心板块异动】（全信源 · 六大板块精选 3-5 条 · 情绪+来源标注）
{chr(10).join(sector_lines)}

### 【🧭 大事件】（近 7 日高重要度 · 国家/全球/政策/地缘）
{chr(10).join(major_lines)}

### 【🔥 今日热点讯息】（非六大板块 · 全信源精选 5 条）
{chr(10).join(hotspot_lines)}

**社媒 / 人气榜（探测）**
{chr(10).join(social_lines)}

### 【✍️ 今日值得开稿 Top 3】（选题雷达 · 标题方向 / 为什么值得写 / 事实锚点）
{chr(10).join(top_story_lines)}
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
        "insights_by_sector": _router_meta.get("insights_by_sector") or {},
        "candidate_diagnostics": _router_meta.get("candidate_diagnostics") or {},
        "router_timing": _router_meta.get("router_timing") or {},
    }
    meta_extra["llm_router_status"] = sections["llm_router"]["status"]
    if sections["llm_router"]["reason"]:
        meta_extra["llm_router_reason"] = sections["llm_router"]["reason"]
    if sections["llm_router"]["menu_count"]:
        meta_extra["llm_router_menu_count"] = sections["llm_router"]["menu_count"]
    if sections["llm_router"]["selected_count"]:
        meta_extra["llm_router_selected_count"] = sections["llm_router"]["selected_count"]
    if sections["llm_router"]["candidate_diagnostics"]:
        meta_extra["llm_router_candidate_diagnostics"] = sections["llm_router"]["candidate_diagnostics"]
    if sections["llm_router"]["router_timing"]:
        meta_extra["llm_router_timing"] = sections["llm_router"]["router_timing"]

    # --- 社交情报分析：量化情绪、热度、恐惧贪婪指数 ---
    all_news_items: list[dict[str, Any]] = []
    # 收集所有新闻条目
    for _bucket in ("items", "items_other_flash"):
        for _it in (sections.get("news") or {}).get(_bucket) or []:
            if isinstance(_it, dict):
                all_news_items.append(_it)
    # 收集板块新闻
    _router_sec = (sections.get("llm_router") or {}).get("items_by_sector") or {}
    if isinstance(_router_sec, dict):
        for _sec_name, _sec_data in _router_sec.items():
            if isinstance(_sec_data, dict):
                for _it in _sec_data.get("items") or []:
                    if isinstance(_it, dict):
                        all_news_items.append(_it)
    # 收集宏观
    for _it in (sections.get("macro_hot") or {}).get("items") or []:
        if isinstance(_it, dict):
            all_news_items.append(_it)
    # 收集社媒
    for _it in (sections.get("social") or {}).get("items") or []:
        if isinstance(_it, dict):
            all_news_items.append(_it)

    unique_intel_items = _dedupe_social_intel_items(all_news_items)

    hist_run_s: list[float] = []
    hist_run_b: list[float] = []
    hist_fg: list[float] = []
    hist_load: dict[str, Any] = {"from_db": False, "loaded_runs": 0}
    max_hist = max(1, _int_env("FINANCE_SOCIAL_INTEL_HISTORY_RUNS", 30))

    if (
        unique_intel_items
        and truthy_env("FINANCE_SOCIAL_INTEL_HISTORY_ENABLED", "1")
    ):
        db_path = _finance_db_path_for_social_hist()
        if db_path.is_file():
            try:
                import sqlite3

                from storage import fetch_social_intel_run_history

                _conn = sqlite3.connect(str(db_path))
                _conn.row_factory = sqlite3.Row
                try:
                    _rows = fetch_social_intel_run_history(
                        _conn, max_hist,
                    )
                    for _r in _rows:
                        hist_run_s.append(float(_r["headline_sentiment"]))
                        hist_run_b.append(float(_r["mean_buzz_score"]))
                        hist_fg.append(float(_r["fear_greed_index"]))
                    hist_load = {
                        "from_db": True,
                        "loaded_runs": len(_rows),
                        "db_path": str(db_path),
                        "max_requested": max_hist,
                    }
                finally:
                    _conn.close()
            except Exception as _ex:
                logger.warning("social_intel DB history load failed: %s", _ex)
                hist_load = {"from_db": False, "error": str(_ex)[:240]}

    if unique_intel_items:
        social_intel_result = enhance_social_intelligence(
            unique_intel_items,
            historical_fg=hist_fg if hist_fg else None,
            historical_run_sentiments=hist_run_s if hist_run_s else None,
            historical_run_buzz=hist_run_b if hist_run_b else None,
        )
    else:
        social_intel_result = {
            "enhanced_items": [],
            "aggregate_metrics": {},
            "stock_sentiments": {},
        }

    _si_field_keys = (
        "sentiment_score",
        "sentiment_label",
        "author_type",
        "author_weight",
        "weighted_sentiment",
        "buzz_score",
        "buzz_zscore",
    )
    _proto_by_key: dict[str, dict[str, Any]] = {}
    for _it in unique_intel_items:
        _dk = _social_intel_item_dedupe_key(_it)
        if _dk:
            _proto_by_key[_dk] = _it
    for _it in all_news_items:
        if not isinstance(_it, dict) or "sentiment_score" in _it:
            continue
        _dk = _social_intel_item_dedupe_key(_it)
        _proto = _proto_by_key.get(_dk)
        if not _proto:
            continue
        for _fk in _si_field_keys:
            if _fk in _proto:
                _it[_fk] = _proto[_fk]

    sections["social_intelligence"] = social_intel_result
    _agg_si = social_intel_result.get("aggregate_metrics") or {}
    meta_extra["social_intelligence"] = {
        "avg_sentiment": _agg_si.get("avg_sentiment"),
        "headline_sentiment": _agg_si.get("headline_sentiment"),
        "platform_weighted_sentiment": _agg_si.get("platform_weighted_sentiment"),
        "sentiment_label": _agg_si.get("sentiment_label"),
        "fear_greed_index": _agg_si.get("fear_greed_index"),
        "fear_greed_label": _agg_si.get("fear_greed_label"),
        "fear_greed_scope": _agg_si.get("fear_greed_scope"),
        "mean_buzz_score": _agg_si.get("mean_buzz_score"),
        "dedupe_input_count": len(all_news_items),
        "dedupe_unique_count": len(unique_intel_items),
        "history": hist_load,
    }

    if (
        unique_intel_items
        and _agg_si
        and truthy_env("FINANCE_SOCIAL_INTEL_HISTORY_ENABLED", "1")
        and truthy_env("FINANCE_SOCIAL_INTEL_HISTORY_APPEND", "1")
    ):
        _dbp = _finance_db_path_for_social_hist()
        try:
            import sqlite3

            from storage import append_social_intel_run_history

            _dbp.parent.mkdir(parents=True, exist_ok=True)
            _c2 = sqlite3.connect(str(_dbp))
            try:
                append_social_intel_run_history(
                    _c2,
                    recorded_at=fetched_at,
                    headline_sentiment=float(_agg_si.get("headline_sentiment") or 0.0),
                    mean_buzz_score=float(_agg_si.get("mean_buzz_score") or 0.0),
                    fear_greed_index=float(_agg_si.get("fear_greed_index") or 50.0),
                    dedupe_unique_count=len(unique_intel_items),
                    source_kind="legacy_pipeline",
                )
            finally:
                _c2.close()
            meta_extra["social_intelligence"]["history_append"] = "ok"
        except Exception as _ex:
            logger.warning("social_intel DB history append failed: %s", _ex)
            meta_extra["social_intelligence"]["history_append"] = f"failed:{str(_ex)[:120]}"
    
    md = _build_live_stream_markdown(sections, errors, fetched_at)
    _rewrite_meta = sections.get("sector_llm_rewrite") or {}
    if _rewrite_meta:
        meta_extra["sector_llm_rewrite_status"] = _rewrite_meta.get("status") or ""
        meta_extra["sector_llm_rewrite_status_by_sector"] = _rewrite_meta.get("status_by_sector") or {}
        meta_extra["sector_llm_rewrite_timing"] = _rewrite_meta.get("timing_by_sector") or {}
        if _rewrite_meta.get("timeout_sec"):
            meta_extra["sector_llm_rewrite_timeout_sec"] = _rewrite_meta.get("timeout_sec")
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
