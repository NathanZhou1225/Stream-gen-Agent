"""编排各 fetcher，生成统一 JSON + markdown_summary。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
from typing import Any

from _common import compute_invariants, now_iso, truthy_env
from fetchers import macro_hot, market, news_rss, social_api, social_scrape_stub
from fetchers.sector_keywords import SECTOR_ORDER


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
    return f"{it.get('title') or ''} {it.get('clean_text') or ''} {it.get('detail') or ''}"


def _is_finance_related(it: dict[str, Any]) -> bool:
    if it.get("sector_tags"):
        return True
    txt = _item_text(it)
    return any(k and k in txt for k in FINANCE_TEXT_HINTS)


def _is_major_event(it: dict[str, Any]) -> bool:
    txt = _item_text(it)
    if any(k and k in txt for k in MAJOR_EVENT_EXCLUDE_HINTS):
        return False
    has_topic = any(k and k in txt for k in MAJOR_EVENT_HINTS)
    has_actor = any(k and k in txt for k in MAJOR_EVENT_ACTOR_HINTS)
    return _is_finance_related(it) and has_topic and has_actor


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
    title = str(it.get("title") or "").strip()
    body = str(it.get("clean_text") or it.get("detail") or title).strip().replace("\n", " ")
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


def _collect_websearch_gaps(sections: dict[str, Any], errors: list[dict[str, Any]]) -> list[dict[str, str]]:
    gaps: list[dict[str, str]] = []
    market_section = sections.get("market") or {}
    nb = market_section.get("northbound") or {}
    nb_val = nb.get("aggregate_net_buy_yi")
    if nb_val is None or (isinstance(nb_val, (int, float)) and abs(float(nb_val)) < 0.0001):
        gaps.append({"area": "北向资金", "reason": "接口为空或返回 0，需联网核验资金流口径"})

    news = sections.get("news") or {}
    by_sec = news.get("items_by_sector") or {}
    if isinstance(by_sec, dict):
        for sec in SECTOR_ORDER:
            if not by_sec.get(sec):
                gaps.append({"area": sec, "reason": "财联社未命中板块正文，需联网补充近期相关事件"})

    macro_items = (sections.get("macro_hot") or {}).get("items") or []
    if not any(isinstance(x, dict) and x.get("detail") and _is_finance_related(x) for x in macro_items):
        gaps.append({"area": "泛财经热点", "reason": "百度热榜无财经详情或未命中财经条目，需 WebSearch 兜底"})

    social_items = (sections.get("social") or {}).get("items") or []
    if not social_items:
        gaps.append({"area": "社媒/人气榜/舆情", "reason": "社媒或人气榜接口为空，需 WebSearch 兜底"})

    failed_codes = {str(e.get("code") or "") for e in errors if isinstance(e, dict)}
    if {"TGB_HOT_FAILED", "WC_RANK_FAILED", "EM_RANK_FAILED"} & failed_codes:
        gaps.append({"area": "人气榜", "reason": "社区/问财/东财接口失败，需 WebSearch 兜底"})

    news_items: list[dict[str, Any]] = []
    news = sections.get("news") or {}
    for bucket in [news.get("items") or [], news.get("items_other_flash") or []]:
        if isinstance(bucket, list):
            news_items.extend([x for x in bucket if isinstance(x, dict)])
    if not any(_is_major_event(x) for x in news_items):
        gaps.append({"area": "国家/全球大事件", "reason": "财联社本轮未命中国家/全球/政策/地缘类大事件，需 WebSearch 补充近期待核验信息"})
    return gaps


def _load_env_with_dotenv() -> dict[str, str]:
    env = dict(os.environ)
    here = Path(__file__).resolve()
    candidates = [
        here.parents[3] / ".env",  # workspace root
        here.parents[4] / ".env",  # ~/.openclaw
        Path("/root/.openclaw/.env"),
    ]
    for path in candidates:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            if key and key not in env:
                env[key] = value
    return env


def _websearch_query(area: str, reason: str) -> str:
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y年%m月%d日")
    text = f"{area} {reason}"
    if "北向" in text:
        return f"{today} 北向资金 沪深港通 净流入 A股"
    if "社媒" in text or "人气榜" in text or "舆情" in text:
        return f"{today} A股 人气榜 热门股票 社媒 舆情"
    if "泛财经" in text or "热点" in text:
        return f"{today} 今日财经热点 A股 港股 宏观"
    if "大事件" in text or "国家" in text or "全球" in text:
        return "近7天 全球宏观 政策 地缘 央行 关税 金融市场 重要事件"
    return f"{today} {area} A股 金融市场"


def _run_tavily_search(area: str, query: str) -> dict[str, Any]:
    here = Path(__file__).resolve()
    tavily_script = here.parents[2] / "liang-tavily-search-1.0.1" / "scripts" / "search.mjs"
    env = _load_env_with_dotenv()
    if not tavily_script.exists():
        return {"area": area, "query": query, "ok": False, "error": f"Tavily script not found: {tavily_script}"}
    if not env.get("TAVILY_API_KEY"):
        return {"area": area, "query": query, "ok": False, "error": "TAVILY_API_KEY not set"}
    base_cmd = ["node", str(tavily_script), query, "-n", "3", "--json"]
    proc = subprocess.run(
        [*base_cmd, "--raw-content"],
        cwd=str(here.parents[3]),
        text=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        env=env,
        check=False,
    )
    stdout_text = proc.stdout.decode("utf-8", errors="ignore")
    stderr_text = proc.stderr.decode("utf-8", errors="ignore")
    if proc.returncode != 0:
        return {"area": area, "query": query, "ok": False, "error": (stderr_text or stdout_text)[-800:]}
    try:
        data = json.loads(stdout_text)
    except json.JSONDecodeError:
        retry = subprocess.run(
            base_cmd,
            cwd=str(here.parents[3]),
            text=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            env=env,
            check=False,
        )
        retry_stdout = retry.stdout.decode("utf-8", errors="ignore")
        retry_stderr = retry.stderr.decode("utf-8", errors="ignore")
        if retry.returncode != 0:
            return {"area": area, "query": query, "ok": False, "error": (retry_stderr or retry_stdout)[-800:]}
        try:
            data = json.loads(retry_stdout)
        except json.JSONDecodeError as exc2:
            return {"area": area, "query": query, "ok": False, "error": f"Tavily JSON parse failed: {exc2}"}
    return {"area": area, "query": query, "ok": True, "data": data}


def _build_tavily_supplement(gaps: list[dict[str, str]]) -> tuple[str, list[dict[str, Any]]]:
    priority = ("北向资金", "社媒/人气榜/舆情", "人气榜", "泛财经热点", "国家/全球大事件")
    ordered = sorted(gaps, key=lambda g: priority.index(g.get("area", "")) if g.get("area", "") in priority else 99)
    planned: list[tuple[str, str]] = []
    seen_queries: set[str] = set()
    for gap in ordered:
        area = str(gap.get("area") or "联网缺口")
        query = _websearch_query(area, str(gap.get("reason") or ""))
        if query in seen_queries:
            continue
        seen_queries.add(query)
        planned.append((area, query))
        if len(planned) >= 4:
            break

    results = [_run_tavily_search(area, query) for area, query in planned]
    if not results:
        return "", []

    fetched_at = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    gap_text = "、".join(str(g.get("area") or "") for g in gaps[:4] if g.get("area"))
    lines = [
        "",
        "### 🔍 联网补充（Tavily 兜底）",
        f"> 触发原因：{gap_text or '部分 API 缺口'}；以下为独立联网补充，不覆盖上方 API 数字。",
    ]
    for item in results:
        area = item.get("area") or "联网补充"
        query = item.get("query") or ""
        lines.append(f"- **{area}**（检索时间：{fetched_at}；查询：{query}）")
        if not item.get("ok"):
            lines.append(f"  - 未执行成功：{item.get('error') or 'unknown error'}")
            continue
        rows = ((item.get("data") or {}).get("results") or [])[:2]
        if not rows:
            lines.append("  - 未找到可核验补充。")
            continue
        for row in rows:
            title = str(row.get("title") or "").strip()
            url = str(row.get("url") or "").strip()
            content = str(row.get("content") or "").strip()
            raw_content = str(row.get("raw_content") or "").strip()
            snippet_src = content or raw_content
            snippet = snippet_src[:220] + ("…" if len(snippet_src) > 220 else "")
            if title and url:
                lines.append(f"  - {title}｜{url}")
            if snippet:
                lines.append(f"    摘要：{snippet}")
            elif title:
                # 避免只剩 URL：无正文时至少给出标题级信息
                lines.append(f"    摘要：该来源标题为“{title}”，原站未返回可截取正文。")
    return "\n".join(lines).rstrip() + "\n", results


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
        return f"- （财联社本轮未命中；行情侧补充）{ '；'.join(hits[:4]) }"
    return f"- （本轮财联社与行情池暂未命中{sec}可用信息）"


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
            src_bits.append("**北向资金**（接口返回 0，疑似未更新/口径异常）：已触发 Agent WebSearch 兜底核验")
        else:
            src_bits.append(f"**北向资金**（尽力探测）：{nb_val} 亿元")
    else:
        src_bits.append("**北向资金**：今日数据暂缺（接口失败或未返回，已触发 Agent WebSearch 兜底核验）")

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
    sector_lines: list[str] = []
    for sec in SECTOR_ORDER:
        sector_lines.append(f"**【{sec}】**")
        lst = by_sec.get(sec) if isinstance(by_sec, dict) else None
        if not lst:
            sector_lines.append(_market_sector_fallback(sec, m))
            continue
        for it in lst[:10]:
            if not isinstance(it, dict):
                continue
            ts_full = str(it.get("published_at") or "").strip()
            hh = news_rss.news_hhmm_for_markdown(str(it.get("published_at") or ""))
            tpart = f"[{ts_full[:19]}]" if len(ts_full) >= 16 else f"[{hh}]"
            title = (it.get("title") or "").strip()
            body = _clip_flash_text(str(it.get("clean_text") or it.get("title") or ""))
            src = str(it.get("sector_line_source") or "tagged")
            src_note = ""
            if src == "recent_keyword":
                src_note = " *〔关键词回溯〕*"
            elif src == "tagged_catchup":
                src_note = " *〔同板块补位〕*"
            if title or body:
                line = f"- {tpart} **{title}** — {body}{src_note}" if title else f"- {tpart} {body}{src_note}"
                sector_lines.append(line)

    all_news_items: list[dict[str, Any]] = []
    for bucket in [n.get("items") or [], n.get("items_other_flash") or []]:
        if isinstance(bucket, list):
            all_news_items.extend([x for x in bucket if isinstance(x, dict)])

    major_lines = _select_weekly_major_lines(
        all_news_items,
        macro_sec.get("items") or [],
        fetched_at=fetched_at,
        limit=5,
    )
    if not major_lines:
        major_lines.append("- （近 7 日未筛出足够高重要度的国家/全球/政策事件，已触发 Agent WebSearch 兜底补充）")

    other = n.get("items_other_flash") or []
    flash_lines: list[str] = []
    if isinstance(other, list) and other:
        for it in other[:10]:
            if not isinstance(it, dict):
                continue
            if not _is_finance_related(it):
                continue
            line = _format_flash_line(it, max_len=180)
            if line:
                flash_lines.append(line)
            if len(flash_lines) >= 8:
                break
    if not flash_lines:
        flash_lines.append("- （财联社其他金融快讯本轮暂无可用条目，已触发 Agent WebSearch 兜底补充）")

    macro_items = macro_sec.get("items") or []
    macro_lines: list[str] = []
    if isinstance(macro_items, list):
        for x in macro_items[:6]:
            if not isinstance(x, dict):
                continue
            t = str(x.get("title") or "").strip()
            det = str(x.get("detail") or "").strip()
            det_clip = _clip_flash_text(det, max_len=320) if det else ""
            if t and det_clip and _is_finance_related(x):
                rk = x.get("rank")
                head = f"- {rk}. **{t}**" if rk is not None else f"- **{t}**"
                macro_lines.append(head)
                if det_clip != t:
                    macro_lines.append(f"  · {det_clip}")

    social_lines: list[str] = []
    s_items = soc.get("items") or []
    if isinstance(s_items, list):
        for it in s_items[:6]:
            if not isinstance(it, dict):
                continue
            title = (it.get("title") or "").strip()
            plat = (it.get("platform") or "").strip()
            if title:
                social_lines.append(f"- [{plat}] {title}" if plat else f"- {title}")
    if not social_lines:
        social_lines.append("- （社媒/人气榜 API 暂不可用或本轮无条目，已触发 Agent WebSearch 兜底补充舆情）")

    error_lines: list[str] = []
    error_cn = {
        "TGB_HOT_FAILED": "淘股吧/社区热榜接口不可用，社媒/人气舆情需 Agent WebSearch 兜底。",
        "WC_RANK_FAILED": "问财/人气榜接口不可用，个股人气需 Agent WebSearch 兜底。",
        "EM_RANK_FAILED": "东方财富人气榜接口异常，个股热度需 Agent WebSearch 兜底。",
        "MACRO_HOT_FINANCE_FILTER_EMPTY": "百度实时热榜未筛出可靠财经条目，泛财经热点改由 Agent WebSearch 兜底。",
        "SOCIAL_WB_HOT_FAILED": "微博热搜接口不可用，社媒舆情需 Agent WebSearch 兜底。",
        "SOCIAL_TGB_OR_HOT_RANK_FAILED": "第二梯队社媒/人气榜接口不可用，社媒舆情需 Agent WebSearch 兜底。",
        "SINA_HK_INDICES_FAILED": "港股指数接口不可用，港股行情需 Agent WebSearch 兜底。",
        "CLS_NODEAPI_FAILED": "财联社宽池接口不可用，板块快讯覆盖可能不足。",
    }
    if errors:
        error_lines.append(f"**告警（中文说明，最多 6 条）**（{len(errors)}）：")
        for e in errors[:6]:
            code = str(e.get("code") or "")
            msg = error_cn.get(code) or str(e.get("message") or "接口调用失败")
            error_lines.append(f"- **{code}**：{msg}")

    markdown = f"""## 📊 今日信源全量快照 ({fetched_at})

### 【📈 大盘与情绪】
{block_market}

### 【🎯 核心板块异动】（财联社 · 按六大板块 + 正文关键词）
{chr(10).join(sector_lines)}

### 【🧭 大事件】（近 7 日高重要度 · 国家/全球/政策/地缘）
{chr(10).join(major_lines)}

### 【🔥 今日热点讯息】（金融相关）
**财联社其他金融快讯（非六大板块）**
{chr(10).join(flash_lines)}

**社媒 / 人气榜（探测）**
{chr(10).join(social_lines)}
"""
    if macro_lines:
        markdown += "\n**百度实时热榜（仅展示有财经详情的条目）**\n" + "\n".join(macro_lines) + "\n"
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

    macro_data, macro_errs = macro_hot.fetch_macro_section(limit=12)
    sections["macro_hot"] = macro_data
    errors.extend(macro_errs)
    if macro_data.get("items"):
        sources_ok.append("macro_hot")

    if "news" in sources:
        data, errs = news_rss.fetch_news_section(keywords, max_items)
        sections["news"] = data
        errors.extend(errs)
        sources_ok.append("news")
        if data.get("keyword_fallback"):
            meta_extra["news_keyword_fallback"] = True
        if data.get("cls_symbol") is not None:
            meta_extra["news_cls_symbol"] = data["cls_symbol"]
        if data.get("sector_filter_fallback"):
            meta_extra["news_sector_filter_fallback"] = True
        if data.get("sector_relax_backfill"):
            meta_extra["news_sector_relax_backfill"] = True

        # 东财热榜关闭时：仅从财联社离线抽词填充 sentiment，便于下游可选展示（不产生告警项）
        if "market_sentiment" in sections.get("market", {}):
            sentiment = sections["market"]["market_sentiment"]
            news_items = data.get("items") or []
            if not sentiment.get("hot_keywords") and not sentiment.get("top_hot_stocks") and news_items:
                extracted_kws = market.extract_keywords_from_news(news_items)
                sections["market"]["market_sentiment"]["hot_keywords"] = extracted_kws
                sections["market"]["market_sentiment"]["hot_keywords_source"] = "offline_news_extract"
                if extracted_kws:
                    sections["market"]["market_sentiment"]["note"] = "热词来自财联社离线抽取（AkShare 社区/东财热榜未返回时的兜底）"

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

    gaps = _collect_websearch_gaps(sections, errors)
    if gaps:
        meta_extra["websearch_required"] = True
        meta_extra["websearch_gaps"] = gaps

    md = _build_live_stream_markdown(sections, errors, fetched_at)
    ok = True
    tavily_supplements: list[dict[str, Any]] = []

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
    if gaps:
        supplement_md, tavily_supplements = _build_tavily_supplement(gaps)
        if supplement_md:
            snapshot["markdown_summary"] = str(snapshot.get("markdown_summary") or "").rstrip() + "\n\n" + supplement_md
            snapshot["meta"]["websearch_executed"] = True
            snapshot["meta"]["websearch_provider"] = "tavily-search"
            snapshot["meta"]["websearch_supplements"] = tavily_supplements
    return snapshot
