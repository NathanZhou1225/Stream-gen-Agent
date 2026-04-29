#!/usr/bin/env python3
"""
带方向开稿前置编排：自然语言方向 → 关键词降维 → 弱耦合调用 finance-source-ingest CLI
→ 读取 snapshot.json → 组装 topic_picking 用 payload（stdout JSON，无 Traceback 外泄）。

设计约束：
- 不 import finance-source-ingest 代码，仅 subprocess；
- 失败时 stdout 仅为结构化 JSON（ok=false），进程退出码 0，便于 Agent 管道解析。
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# streamy-content-gen 根目录（本文件位于 scripts/）
SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
# 与 finance-source-ingest 为同 skills/ 下兄弟目录（可迁移：仅依赖此相对布局）
DEFAULT_FINANCE_SIBLING = SKILL_ROOT.parent / "finance-source-ingest"
# 默认写入 workspace 内，避免沙箱/权限导致的 /tmp 写入失败
DEFAULT_OUT_DIR = SKILL_ROOT.parent.parent / "tmp" / "finance_data"
PROVENANCE = "finance-source-ingest|preflight_topic.py"
# markdown 用于候选抽取的软上限（降上下文体积，减少 topic 阶段耗时）
MAX_MD_CHARS = 1600
# 注入 source_context 的事实短摘条数（避免把整段 markdown 喂给后续阶段）
SOURCE_CONTEXT_BULLET_CAP = 8
# 飞书/对话可见的「热点摘要」条数（与 SKILL 契约一致，控制篇幅）
FEISHU_DIGEST_MAX = 8
FEISHU_DIGEST_LINE_CHARS = 220
HOTLIST_DOWN_NOTICE = "⚠️ 弱舆情热榜信号暂不可用（两路来源均失败），本轮选题基于行情/快讯/公告生成。"
CST = timezone(timedelta(hours=8))
HOT_RANK_RETRY = 2

DOMAIN_LEXICON: dict[str, tuple[str, ...]] = {
    "ai": ("人工智能", "AI", "算力", "芯片", "服务器", "大模型", "智算", "GPU", "半导体"),
    "macro_policy": ("政策", "政治局", "发改委", "央行", "货币", "财政", "基建", "会议"),
    "market_sentiment": ("情绪", "节前", "缩量", "成交额", "热点", "主线", "风险偏好", "避险"),
    "energy_new": ("新能源", "光伏", "风电", "储能", "锂电", "电池", "充电桩"),
    "healthcare": ("医药", "医疗", "创新药", "器械", "医保", "集采"),
    "military": ("军工", "国防", "航天", "导弹", "卫星", "船舶"),
    "teacher_focus": ("科技", "新能源", "港股", "黄金", "银行", "有色"),
}
STRICT_DOMAIN_TAGS = set()


def _now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def _emit(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _exit_ok(obj: dict[str, Any]) -> None:
    _emit(obj)
    raise SystemExit(0)


def _keywords_from_direction(direction: str) -> str:
    """
    极简关键词降维：去标点 → 按空白切分 → 取长度≥2 的前若干 token；
    若过短则退回整句截断，供 ingest --keywords 使用（空格分隔）。
    """
    raw = (direction or "").strip()
    if not raw:
        return "市场"
    s = re.sub(r"[，。、；;:!！?？\"“”'‘（）()\[\]【】\s]+", " ", raw)
    parts = [p.strip() for p in s.split() if len(p.strip()) >= 2]
    parts = parts[:12]
    if not parts:
        return raw[:48] if raw else "市场"
    return " ".join(parts)


def _detect_domain_tags(direction: str) -> list[str]:
    d = direction or ""
    tags: list[str] = []
    for tag, kws in DOMAIN_LEXICON.items():
        if any(k in d for k in kws):
            tags.append(tag)
    if "teacher_focus" not in tags:
        tags.append("teacher_focus")
    if not tags:
        tags.append("market_sentiment")
    return tags


def _expand_keywords(direction: str) -> tuple[str, list[str], list[str]]:
    base = _keywords_from_direction(direction)
    tags = _detect_domain_tags(direction)
    expanded = []
    for t in tags:
        expanded.extend(DOMAIN_LEXICON.get(t, ()))
    seed = [x for x in base.split() if x.strip()]
    all_kw: list[str] = []
    for x in [*seed, *expanded]:
        if x and x not in all_kw:
            all_kw.append(x)
        if len(all_kw) >= 14:
            break
    if not all_kw:
        all_kw = ["市场"]
    return " ".join(all_kw), tags, all_kw


def _feishu_digest_bullets(md_summary: str, max_items: int = FEISHU_DIGEST_MAX) -> list[str]:
    """
    从 markdown_summary 抽取短列表，供飞书选题轮「必展示」事实锚点（不等同于整段 source_context）。
    规则：顺序扫描以 "- " 开头的行，直至 max_items；无列表行时退回首段非标题正文一行。
    """
    md = (md_summary or "").strip()
    if not md:
        return []
    bullets: list[str] = []
    for raw in md.splitlines():
        s = raw.strip()
        if s.startswith("- ") and len(s) > 2:
            bullets.append(s[:FEISHU_DIGEST_LINE_CHARS])
            if len(bullets) >= max_items:
                break
    if bullets:
        return bullets
    # 无 markdown 列表时的降级：取首个有实质内容的非标题行
    for raw in md.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        return [s[:FEISHU_DIGEST_LINE_CHARS]]
    return []


def _resolve_ingest(finance_root: Path) -> Path:
    return finance_root / "scripts" / "ingest.py"


def _resolve_hot_rank_fetcher() -> Path:
    return SCRIPTS_DIR / "fetch_hot_rank.py"


def _resolve_finance_venv_python(finance_root: Path) -> str:
    """Prefer finance-source-ingest/.venv if present (Unix + Windows layouts)."""
    if platform.system() == "Windows":
        win = finance_root / ".venv" / "Scripts" / "python.exe"
        if win.is_file():
            return str(win)
    nix = finance_root / ".venv" / "bin" / "python"
    if nix.is_file():
        return str(nix)
    return sys.executable


def _markdown_bullet_lines(md: str, cap: int = 32) -> list[str]:
    """取 markdown 中以 '- ' 开头的行（截断），保序、去重前缀避免完全重复。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in (md or "").splitlines():
        s = raw.strip()
        if not s.startswith("- ") or len(s) < 3:
            continue
        key = re.sub(r"\s+", "", s[:40])
        if key in seen:
            continue
        seen.add(key)
        out.append(s[:280])
        if len(out) >= cap:
            break
    return out


def _compact_source_context_bullets(raw_bullets: list[str], cap: int = SOURCE_CONTEXT_BULLET_CAP) -> list[str]:
    """将原始 bullets 压缩为短摘，避免 topic_candidates.json 体积过大。"""
    out: list[str] = []
    seen: set[str] = set()
    for line in raw_bullets:
        if _is_noise_bullet(line) or _is_placeholder_line(line):
            continue
        brief = _title_from_bullet_line(line, max_len=72).strip()
        if not brief:
            continue
        key = _norm_title_key(brief)
        if key in seen:
            continue
        seen.add(key)
        out.append(f"- {brief}")
        if len(out) >= cap:
            break
    return out


def _is_index_or_flow_bullet(line: str) -> bool:
    """指数/北向/成交额等「打底」行，与财联社叙事行区分，用于选题差异化。"""
    s = line.strip()
    return any(
        k in s
        for k in (
            "上证指数",
            "深证成指",
            "创业板指",
            "北向",
            "成交额",
            "沪深京三市",
            "三大指数",
            "恒生",
            "道琼斯",
            "主力资金",
            "涨停",
            "跌停",
        )
    )


def _is_noise_bullet(line: str) -> bool:
    """ingest 告警/降级/异常栈片段，不作为选题标题与 evidence 首选。"""
    s = line.strip()
    low = s.lower()
    if any(
        x in low
        for x in (
            "fallback_sina",
            "akshare_call_failed",
            "connectionerror",
            "protocolerror",
            "remotedisconnected",
            "timeouterror",
            "[a_share_indices",
            "[news_keyword_fallback",
            "keyword_fallback",
        )
    ):
        return True
    return s.startswith("- [") and ("超时" in s or "失败" in s or "error" in low)


def _is_placeholder_line(line: str) -> bool:
    s = line.strip()
    low = s.lower()
    return any(x in s for x in ("暂无数据", "接口异常", "未获取到数据")) or ("not_available" in low)


def _title_from_bullet_line(line: str, max_len: int = 56) -> str:
    """从单条 '- …' 行抽「钩子标题」，尽量去掉时间戳与社媒前缀。"""
    body = line[2:].strip() if line.strip().startswith("- ") else line.strip()
    body = re.sub(r"^\[\d{1,2}:\d{2}\]\s*", "", body)
    if "】" in body:
        tail = body.split("】", 1)[-1].strip()
        if len(tail) >= 6:
            body = tail
    if "—" in body and len(body) > 36:
        tail = body.split("—", 1)[-1].strip()
        if len(tail) >= 6:
            body = tail
    # 财联社常见前缀过长时，取「电，」后正文
    if "财联社" in body[:16] and "电，" in body:
        parts = body.split("电，", 1)
        if len(parts) == 2 and len(parts[1].strip()) >= 6:
            body = parts[1].strip()
    body = re.sub(r"\s+", " ", body).strip()
    if len(body) > max_len:
        body = body[: max_len - 1].rstrip("，、； ") + "…"
    return body[:80]


def _norm_title_key(title: str) -> str:
    return re.sub(r"\s+", "", (title or "")[:20])


def _direction_tokens(direction: str) -> set[str]:
    """与 ingest 关键词一致：长度≥2 的 token，用于判断快讯是否与用户方向同主题。"""
    kw = _keywords_from_direction(direction)
    return {t for t in kw.split() if len(t) >= 2}


def _bullet_touches_direction(line: str, direction: str) -> bool:
    toks = _direction_tokens(direction)
    if not toks:
        return True
    return any(t in line for t in toks)


def _relevance_score(text: str, direction: str) -> float:
    toks = _direction_tokens(direction)
    if not toks:
        return 1.0
    hit = sum(1 for t in toks if t in (text or ""))
    return hit / max(1, len(toks))


def _three_distinct_candidates(direction: str, raw_bullets: list[str]) -> list[dict[str, str]]:
    """
    三条候选须在标题上可区分：优先用不同快讯/事实行；不足时用「行情 / 对照 / 讲述结构」模板。
    """
    direction_short = (direction or "").strip()[:44] or "本期选题"
    angles = [
        "偏事实钩子：用本条快讯或数据开场，再扣回用户方向",
        "偏结构对照：与上条不同维度（板块/资金/节奏），避免重复口径",
        "偏讲述策略：把用户方向落成「观众能跟上的三段节奏」",
    ]

    def pack(anchor_line: str, title: str, angle: str) -> dict[str, str]:
        a = anchor_line[:280]
        return {
            "title": title[:80],
            "angle": angle,
            "evidence_anchor": f"{a} | provenance: {PROVENANCE}",
        }

    if not raw_bullets:
        return [
            pack(direction_short, direction_short[:80], angles[0]),
            pack(direction_short, f"{direction_short}：先讲预期再看盘面", angles[1]),
            pack(direction_short, f"{direction_short}：三个节奏误区一次说清", angles[2]),
        ]

    clean = [b for b in raw_bullets if (not _is_noise_bullet(b) and not _is_placeholder_line(b))]
    if not clean:
        clean = [b for b in raw_bullets if not _is_noise_bullet(b)] or list(raw_bullets)
    news = [b for b in clean if not _is_index_or_flow_bullet(b)]
    idx = [b for b in clean if _is_index_or_flow_bullet(b)]

    seen_keys: set[str] = set()

    def unique_title(raw_title: str, fallback: str) -> str:
        t = (raw_title or "").strip()[:80] or fallback[:80]
        k = _norm_title_key(t)
        if k and k not in seen_keys:
            seen_keys.add(k)
            return t
        for suf in ("·对照", "·节奏", "·落地", "·追问", "·复盘"):
            tt = (t[:72] + suf)[:80]
            kk = _norm_title_key(tt)
            if kk not in seen_keys:
                seen_keys.add(kk)
                return tt
        tt = (fallback[:74] + "·选")[:80]
        seen_keys.add(_norm_title_key(tt))
        return tt

    triples: list[tuple[str, str, str]] = []

    # ① 快讯优先；无快讯则用首条列表
    a1 = news[0] if news else raw_bullets[0]
    raw_headline = _title_from_bullet_line(a1)
    if news and not _bullet_touches_direction(a1, direction):
        snippet = raw_headline[:34] + ("…" if len(raw_headline) > 34 else "")
        blended = f"{direction_short[:30]}｜盘面热点：{snippet}"
        blended = (blended[:77] + "…") if len(blended) > 80 else blended
        t1 = unique_title(blended, raw_headline)
    else:
        t1 = unique_title(raw_headline, direction_short)
    triples.append((a1, t1, angles[0]))

    # ② 第二条快讯，或指数/北向，或列表第二行
    if len(news) > 1:
        a2 = news[1]
    elif idx:
        a2 = idx[0]
    elif len(raw_bullets) > 1:
        a2 = raw_bullets[1]
    else:
        a2 = raw_bullets[0]
    raw_t2 = _title_from_bullet_line(a2)
    if _norm_title_key(raw_t2) == _norm_title_key(triples[0][1]):
        raw_t2 = f"对照｜{_title_from_bullet_line(a2, 40)}"
    t2 = unique_title(raw_t2, f"行情打底｜{direction_short[:36]}")
    triples.append((a2, t2, angles[1]))

    # ③ 第三条快讯；否则「指数+用户方向」模板；再否则轮换列表靠后行
    if len(news) > 2:
        a3 = news[2]
        raw_t3 = _title_from_bullet_line(a3)
    elif idx:
        a3 = idx[min(1, len(idx) - 1)]
        raw_t3 = f"指数走弱时，「{direction_short[:30]}」怎么讲才不空"
    else:
        a3 = raw_bullets[min(2, len(raw_bullets) - 1)]
        raw_t3 = _title_from_bullet_line(a3)
    if _norm_title_key(raw_t3) in seen_keys:
        raw_t3 = f"{direction_short}：预期→盘面→行动，一条线讲透"
    t3 = unique_title(raw_t3, f"讲述顺序｜{direction_short[:36]}")
    triples.append((a3, t3, angles[2]))

    return [pack(a, t, ang) for a, t, ang in triples]


def _source_type_from_line(line: str) -> str:
    s = line.lower()
    if "热榜" in line or "微博" in line or "抖音" in line or "知乎" in line or "百度" in line:
        return "hotlist"
    if "财联社" in line or line.strip().startswith("- ["):
        return "news_flash"
    if any(k in s for k in ("指数", "北向", "成交额", "涨跌", "人气股")):
        return "market"
    return "announcement"


def _confidence_for_source(source_type: str) -> str:
    if source_type in ("market", "news_flash", "announcement"):
        return "high"
    if source_type == "hotlist":
        return "medium"
    return "low"


def _source_ref_from_line(line: str) -> str:
    s = line.strip()
    m = re.match(r"^- \[(\d{1,2}:\d{2})\]", s)
    if m:
        return f"财联社 {m.group(1)}"
    if "微博" in s:
        return "热榜:微博"
    if "抖音" in s:
        return "热榜:抖音"
    if "知乎" in s:
        return "热榜:知乎"
    if "百度" in s:
        return "热榜:百度"
    if "指数" in s or "北向" in s or "成交额" in s:
        return "market_snapshot"
    return "ingest_markdown_summary"


def _build_evidence_for_candidate(
    raw_bullets: list[str],
    weak_sentiment: dict[str, Any] | None,
    anchor_line: str,
    direction: str,
    domain_lines: list[str] | None = None,
) -> list[dict[str, str]]:
    pool = [x for x in raw_bullets if (not _is_noise_bullet(x) and not _is_placeholder_line(x))]
    if not pool:
        pool = [x for x in raw_bullets if not _is_noise_bullet(x)]
    domain_pool = [x for x in (domain_lines or []) if (not _is_noise_bullet(x) and not _is_placeholder_line(x))]
    touched = [x for x in pool if _bullet_touches_direction(x, direction)]
    picked: list[str] = []
    if anchor_line and (not _is_placeholder_line(anchor_line)):
        picked.append(anchor_line)
    for line in domain_pool + touched + pool:
        if len(picked) >= 3:
            break
        key = _norm_title_key(line)
        if any(_norm_title_key(x) == key for x in picked):
            continue
        picked.append(line)

    non_index_pool = [x for x in (domain_pool + touched + pool) if not _is_index_or_flow_bullet(x)]
    non_index_count = sum(1 for x in picked if not _is_index_or_flow_bullet(x))
    if non_index_count < 2 and non_index_pool:
        for line in non_index_pool:
            if non_index_count >= 2:
                break
            if any(_norm_title_key(x) == _norm_title_key(line) for x in picked):
                continue
            if len(picked) < 3:
                picked.append(line)
                non_index_count += 1
                continue
            # 用非指数条替换掉末尾指数/北向等通用条，降低论据同质化
            for i in range(len(picked) - 1, -1, -1):
                if _is_index_or_flow_bullet(picked[i]):
                    picked[i] = line
                    non_index_count += 1
                    break

    out: list[dict[str, str]] = []
    for line in picked:
        point = _title_from_bullet_line(line, max_len=88)
        stype = _source_type_from_line(line)
        out.append(
            {
                "point": point,
                "source_type": stype,
                "source_ref": _source_ref_from_line(line),
                "confidence": _confidence_for_source(stype),
            }
        )
    if weak_sentiment and len(out) < 3:
        out.append(
            {
                "point": str(weak_sentiment.get("note") or "热榜出现相关讨论热度"),
                "source_type": "hotlist",
                "source_ref": str(weak_sentiment.get("source") or "hotlist"),
                "confidence": "medium",
            }
        )
    while len(out) < 3:
        out.append(
            {
                "point": "盘面与快讯暂无新增强信号，建议以已确认事实为主线展开。",
                "source_type": "market",
                "source_ref": "preflight_fallback",
                "confidence": "low",
            }
        )
    return out[:3]


def _domain_specific_line(raw_bullets: list[str], tags: list[str]) -> str | None:
    pool = [x for x in raw_bullets if (not _is_noise_bullet(x) and not _is_placeholder_line(x))]
    if not pool:
        return None
    domain_words: list[str] = []
    for t in tags:
        domain_words.extend(DOMAIN_LEXICON.get(t, ()))
    for line in pool:
        if any(w in line for w in domain_words):
            return line
    return None


def _collect_domain_lines(snapshot: dict[str, Any], raw_bullets: list[str], tags: list[str]) -> list[str]:
    domain_words: list[str] = []
    for t in tags:
        domain_words.extend(DOMAIN_LEXICON.get(t, ()))
    domain_words = [w for w in domain_words if w]
    if not domain_words:
        return []

    out: list[str] = []
    seen: set[str] = set()

    def _push(line: str) -> None:
        s = (line or "").strip()
        if not s:
            return
        if _is_noise_bullet(s) or _is_placeholder_line(s):
            return
        key = _norm_title_key(s)
        if key in seen:
            return
        if any(w in s for w in domain_words):
            seen.add(key)
            out.append(s)

    for b in raw_bullets:
        _push(b)

    news_items = (((snapshot.get("sections") or {}).get("news") or {}).get("items") or [])
    if isinstance(news_items, list):
        for it in news_items:
            if not isinstance(it, dict):
                continue
            title = str(it.get("title") or "").strip()
            if not title:
                continue
            _push(f"- {title}")

    return out


def _domain_enhanced_bullets(snapshot: dict[str, Any], tags: list[str]) -> list[str]:
    """从结构化快照抽领域细节，补齐 markdown 摘要遗漏。"""
    domain_words: list[str] = []
    for t in tags:
        domain_words.extend(DOMAIN_LEXICON.get(t, ()))
    domain_words = [w for w in domain_words if w]
    if not domain_words:
        return []

    out: list[str] = []
    seen: set[str] = set()

    def _push(line: str) -> None:
        s = (line or "").strip()
        if not s:
            return
        if _is_noise_bullet(s) or _is_placeholder_line(s):
            return
        key = _norm_title_key(s)
        if not key or key in seen:
            return
        if any(w in s for w in domain_words):
            seen.add(key)
            out.append(s[:280])

    sections = snapshot.get("sections") or {}
    market = sections.get("market") or {}

    rank_items = ((market.get("industry_rank") or {}).get("items") or [])
    if isinstance(rank_items, list):
        for row in rank_items[:8]:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            pct = row.get("pct_change")
            inflow = row.get("main_net_inflow")
            msg = f"- 行业资金：{name}"
            if isinstance(pct, (int, float)):
                msg += f" 涨跌幅 {pct:+.2f}%"
            if isinstance(inflow, (int, float)):
                msg += f"，主力净流入 {inflow:+.2f} 亿"
            _push(msg)

    temp_items = ((market.get("market_temperature") or {}).get("top_inflow_sectors") or [])
    if isinstance(temp_items, list):
        for row in temp_items[:6]:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            val = row.get("main_net_inflow_yi")
            msg = f"- 资金风向：{name}"
            if isinstance(val, (int, float)):
                msg += f" 主力净流入 {val:+.2f} 亿"
            _push(msg)

    news_items = ((sections.get("news") or {}).get("items") or [])
    if isinstance(news_items, list):
        for it in news_items[:20]:
            if not isinstance(it, dict):
                continue
            title = str(it.get("title") or "").strip()
            if title:
                _push(f"- {title}")
            clean = str(it.get("clean_text") or "").strip()
            if clean:
                _push(f"- 快讯要点：{clean[:90].rstrip('，、； ')}")

    return out[:24]


def _focus_sector_bullets(snapshot: dict[str, Any]) -> list[str]:
    rank_items = (((snapshot.get("sections") or {}).get("market") or {}).get("industry_rank") or {}).get("items") or []
    if not isinstance(rank_items, list):
        return []
    out: list[str] = []
    for row in rank_items:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        if not any(k in name for k in DOMAIN_LEXICON.get("teacher_focus", ())):
            continue
        pct = row.get("pct_change")
        if isinstance(pct, (int, float)):
            out.append(f"- 重点板块：{name} 今日涨跌幅 {pct:+.2f}%")
        else:
            out.append(f"- 重点板块：{name}")
    return out[:6]


def _candidate_relevance(c: dict[str, Any], direction: str) -> float:
    title = str(c.get("title") or "")
    thesis = str(c.get("thesis") or "")
    ev = c.get("evidence") or []
    ev_text = " ".join(str((x or {}).get("point") or "") for x in ev if isinstance(x, dict))
    base = f"{title} {thesis} {ev_text}".strip()
    return _relevance_score(base, direction)


def _pick_tophub_signal(hot_rank: dict[str, Any]) -> dict[str, Any] | None:
    lists = hot_rank.get("lists") or []
    if not isinstance(lists, list):
        return None
    for lst in lists:
        if not isinstance(lst, dict):
            continue
        site = str(lst.get("site") or "").strip()
        items = lst.get("items") or []
        if not site or not items or not isinstance(items, list):
            continue
        first = items[0] if isinstance(items[0], dict) else {}
        title = str(first.get("title") or "").strip()
        if not title:
            continue
        rank = first.get("rank")
        return {
            "source": f"tophub:{site}",
            "signal": "staying_hot",
            "rank_or_heat": rank,
            "ts": str(hot_rank.get("as_of") or _now_iso()),
            "note": f"{site}热榜出现相关讨论：{title}",
        }
    return None


def _pick_social_signal(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    social = ((snapshot.get("sections") or {}).get("social") or {})
    items = social.get("items") or []
    if not isinstance(items, list) or not items:
        return None
    first = items[0] if isinstance(items[0], dict) else {}
    title = str(first.get("title") or "").strip()
    platform = str(first.get("platform") or "").strip() or "social"
    if not title:
        return None
    return {
        "source": f"social:{platform}",
        "signal": "staying_hot",
        "rank_or_heat": str(first.get("heat") or "") or None,
        "ts": str(social.get("as_of") or _now_iso()),
        "note": f"{platform}相关热度信号：{title}",
    }


def _build_hotlist_context(hot_rank: dict[str, Any], snapshot: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    signal_primary = _pick_tophub_signal(hot_rank)
    signal_secondary = _pick_social_signal(snapshot)
    if signal_primary:
        return (
            {
                "primary_source": "tophub.today",
                "secondary_source": "social_api(vvhan->akshare)",
                "fallback_used": False,
                "fallback_reason": "",
            },
            signal_primary,
            None,
        )
    if signal_secondary:
        return (
            {
                "primary_source": "tophub.today",
                "secondary_source": "social_api(vvhan->akshare)",
                "fallback_used": True,
                "fallback_reason": "primary_source_unavailable",
            },
            signal_secondary,
            None,
        )
    return (
        {
            "primary_source": "tophub.today",
            "secondary_source": "social_api(vvhan->akshare)",
            "fallback_used": True,
            "fallback_reason": "both_sources_unavailable",
        },
        None,
        HOTLIST_DOWN_NOTICE,
    )


def _classify_failure(reason_or_code: str) -> str:
    s = (reason_or_code or "").lower()
    if any(x in s for x in ("name or service not known", "gaierror", "dns", "nodename nor servname")):
        return "DNS_FAIL"
    if any(x in s for x in ("ip 受限", "forbidden", "403", "access denied")):
        return "IP_LIMIT"
    if any(x in s for x in ("attributeerror", "schema", "no attribute")):
        return "API_SCHEMA_CHANGE"
    if any(x in s for x in ("empty", "无数据", "not_available", "返回空")):
        return "EMPTY_DATA"
    if any(x in s for x in ("timeout", "timed out")):
        return "TIMEOUT"
    return "UNKNOWN"


def _first_error_by_source(snapshot: dict[str, Any], source: str) -> dict[str, Any] | None:
    errs = snapshot.get("errors") or []
    if not isinstance(errs, list):
        return None
    for e in errs:
        if isinstance(e, dict) and str(e.get("source") or "") == source:
            return e
    return None


def _pick_tophub_error(hot_rank: dict[str, Any]) -> dict[str, Any] | None:
    errs = hot_rank.get("errors") or []
    if not isinstance(errs, list):
        return None
    for e in errs:
        if isinstance(e, dict):
            return e
    return None


def _build_source_availability(snapshot: dict[str, Any], hot_rank: dict[str, Any]) -> tuple[dict[str, Any], str]:
    sections = snapshot.get("sections") or {}
    market_ok = bool(((sections.get("market") or {}).get("a_share_indices") or {}).get("items"))
    news_ok = bool((sections.get("news") or {}).get("items"))
    social_ok = bool((sections.get("social") or {}).get("items"))
    tophub_ok = bool(hot_rank.get("lists"))

    available: list[str] = []
    degraded: list[dict[str, str]] = []
    unavailable: list[dict[str, str]] = []

    if market_ok:
        available.append("market")
        e = _first_error_by_source(snapshot, "market")
        if isinstance(e, dict):
            raw = f"{e.get('code') or ''} {e.get('message') or ''}"
            degraded.append({"source": "market", "failure_type": _classify_failure(raw), "detail": str(e.get("code") or "market_degraded")})
    else:
        e = _first_error_by_source(snapshot, "market") or {}
        raw = f"{e.get('code') or ''} {e.get('message') or ''}"
        unavailable.append({"source": "market", "failure_type": _classify_failure(raw), "detail": str(e.get("code") or "market_unavailable")})

    if news_ok:
        available.append("news")
        e = _first_error_by_source(snapshot, "news")
        if isinstance(e, dict):
            raw = f"{e.get('code') or ''} {e.get('message') or ''}"
            degraded.append({"source": "news", "failure_type": _classify_failure(raw), "detail": str(e.get("code") or "news_degraded")})
    else:
        e = _first_error_by_source(snapshot, "news") or {}
        raw = f"{e.get('code') or ''} {e.get('message') or ''}"
        unavailable.append({"source": "news", "failure_type": _classify_failure(raw), "detail": str(e.get("code") or "news_unavailable")})

    if social_ok:
        available.append("social")
        e = _first_error_by_source(snapshot, "social")
        if isinstance(e, dict):
            raw = f"{e.get('code') or ''} {e.get('message') or ''}"
            degraded.append({"source": "social", "failure_type": _classify_failure(raw), "detail": str(e.get("code") or "social_degraded")})
    else:
        e = _first_error_by_source(snapshot, "social") or {}
        raw = f"{e.get('code') or ''} {e.get('message') or ''}"
        unavailable.append({"source": "social", "failure_type": _classify_failure(raw), "detail": str(e.get("code") or "social_unavailable")})

    if tophub_ok:
        available.append("tophub")
        e = _pick_tophub_error(hot_rank)
        if isinstance(e, dict):
            raw = f"{e.get('item') or ''} {e.get('reason') or ''}"
            degraded.append({"source": "tophub", "failure_type": _classify_failure(raw), "detail": str(e.get("item") or "tophub_degraded")})
    else:
        e = _pick_tophub_error(hot_rank) or {}
        raw = f"{e.get('item') or ''} {e.get('reason') or ''}"
        unavailable.append({"source": "tophub", "failure_type": _classify_failure(raw), "detail": str(e.get("item") or "tophub_unavailable")})

    avail_text = "、".join(available) if available else "无"
    unavail_text = (
        "、".join(f"{x['source']}({x['failure_type']})" for x in unavailable)
        if unavailable
        else "无"
    )
    degraded_text = (
        "、".join(f"{x['source']}({x['failure_type']})" for x in degraded)
        if degraded
        else "无"
    )
    feishu_notice = f"🔎 信源状态：可用[{avail_text}]；降级[{degraded_text}]；不可用[{unavail_text}]。"
    return {"available": available, "degraded": degraded, "unavailable": unavailable}, feishu_notice


def _build_topic_payload(
    direction: str,
    snapshot: dict[str, Any],
    md_summary: str,
    hot_rank: dict[str, Any],
    domain_tags: list[str],
) -> dict[str, Any]:
    """将 ingest 快照压成 topic_picking 所需最小契约（与 draft_manager P0-B 对齐）。"""
    md_trim = (md_summary or "")[:MAX_MD_CHARS].strip()
    meta = snapshot.get("meta") or {}
    raw_bullets = _markdown_bullet_lines(md_trim)
    raw_bullets.extend(_focus_sector_bullets(snapshot))
    domain_enhanced = _domain_enhanced_bullets(snapshot, domain_tags)
    raw_bullets.extend(domain_enhanced)
    base_candidates = _three_distinct_candidates(direction, raw_bullets)
    hotlist_meta, weak_sentiment, down_notice = _build_hotlist_context(hot_rank, snapshot)
    source_availability, source_notice = _build_source_availability(snapshot, hot_rank)
    direction_short = (direction or "").strip()[:32] or "本期方向"
    domain_lines = _collect_domain_lines(snapshot, raw_bullets, domain_tags)
    domain_hint = domain_lines[0] if domain_lines else None
    candidates: list[dict[str, Any]] = []
    for c in base_candidates:
        title = str(c.get("title") or "").strip()
        anchor = str(c.get("evidence_anchor") or "")
        thesis = f"{direction_short}相关讨论与盘面事实正在共振，优先围绕「{title[:24]}」展开。"
        candidates.append(
            {
                "title": title,
                "angle": c.get("angle"),
                "thesis": thesis[:88],
                "evidence": _build_evidence_for_candidate(raw_bullets, weak_sentiment, anchor, direction, domain_lines=domain_lines),
                "weak_sentiment": weak_sentiment,
                "evidence_anchor": anchor,
            }
        )
    # T2c: 每个候选至少补 1 条同域专属论据（若可命中）
    domain_line = _domain_specific_line(raw_bullets, domain_tags) or domain_hint
    if domain_line:
        for c in candidates:
            ev = c.get("evidence") or []
            if not isinstance(ev, list) or not ev:
                continue
            if any(any(k in str((x or {}).get("point") or "") for k in DOMAIN_LEXICON.get(t, ())) for x in ev for t in domain_tags):
                continue
            ev[0] = {
                "point": _title_from_bullet_line(domain_line, max_len=88),
                "source_type": _source_type_from_line(domain_line),
                "source_ref": _source_ref_from_line(domain_line),
                "confidence": _confidence_for_source(_source_type_from_line(domain_line)),
            }
    # 严格领域（当前可按需开启）若完全没有同域证据，阻断选题输出，避免“泛盘面硬凑”
    if STRICT_DOMAIN_TAGS and any(t in STRICT_DOMAIN_TAGS for t in domain_tags):
        has_domain_evidence = False
        for c in candidates:
            ev = c.get("evidence") or []
            if not isinstance(ev, list):
                continue
            txt = " ".join(str((x or {}).get("point") or "") for x in ev if isinstance(x, dict))
            if any(any(w in txt for w in DOMAIN_LEXICON.get(t, ())) for t in domain_tags):
                has_domain_evidence = True
                break
        if not has_domain_evidence:
            raise ValueError(
                "domain evidence insufficient: 当前信源未命中该方向的同域事实（如 AI/算力/芯片）。已阻断候选输出，请补充更具体领域线索或稍后重试。"
            )

    # T2b: 方向相关性守门（低相关则重写为方向前缀并二次校验）
    scores = [_candidate_relevance(x, direction) for x in candidates]
    avg_score = sum(scores) / max(1, len(scores))
    if avg_score < 0.20:
        for i, c in enumerate(candidates, start=1):
            title = str(c.get("title") or "")
            if _relevance_score(title, direction) < 0.20:
                c["title"] = f"{direction_short}｜{title[:42]}".strip("｜")
            c["thesis"] = f"围绕「{direction_short}」给出第{i}条可执行讲法，论据与当日盘面/快讯保持同域。"
        scores = [_candidate_relevance(x, direction) for x in candidates]
        avg_score = sum(scores) / max(1, len(scores))
    if avg_score < 0.14:
        raise ValueError("topic relevance too low: 候选与用户方向相关性不足，请补充更具体方向或稍后重试")

    compact_facts = _compact_source_context_bullets(raw_bullets, cap=SOURCE_CONTEXT_BULLET_CAP)
    source_context: list[str] = [
        f"【用户方向】{direction.strip()}",
        f"【ingest 关键词】{meta.get('keywords')}",
        f"【领域标签】{', '.join(domain_tags)}",
        ("【事实短摘】\n" + "\n".join(compact_facts)) if compact_facts else "【事实短摘】（空）",
        f"provenance: {PROVENANCE}",
    ]
    if down_notice:
        source_context.append(f"【弱舆情提示】{down_notice}")
    source_context.append(f"【信源状态】{source_notice}")
    if domain_hint:
        source_context.append(f"【领域证据】{_title_from_bullet_line(domain_hint, max_len=88)}")
    if domain_enhanced:
        source_context.append(f"【领域增强来源】共抽取 {len(domain_enhanced)} 条同域细节（行业/资金风向/快讯要点）")

    return {
        "version": "topic_schema_v1",
        "direction": direction.strip(),
        "generated_at": _now_iso(),
        "hotlist_meta": hotlist_meta,
        "source_context": source_context,
        "candidates": candidates,
        "preflight_meta": {
            "direction": direction.strip(),
            "ingest_schema_version": snapshot.get("schema_version"),
            "ingest_fetched_at": meta.get("fetched_at"),
            "markdown_truncated": len(md_summary or "") > MAX_MD_CHARS,
            "feishu_notice": down_notice,
            "feishu_source_notice": source_notice,
            "source_availability": source_availability,
            "relevance_scores": scores,
            "relevance_avg": round(avg_score, 4),
            "domain_tags": domain_tags,
            "source_context_compact": True,
            "source_context_bullet_count": len(compact_facts),
        },
    }


def main() -> None:
    p = argparse.ArgumentParser(description="带方向开稿：拉取 finance-source-ingest 事实并输出 topic_payload JSON")
    p.add_argument("--direction", required=True, help="用户自然语言选题方向")
    p.add_argument(
        "--finance-root",
        type=Path,
        default=DEFAULT_FINANCE_SIBLING,
        help="finance-source-ingest 技能根目录（默认同 workspace 兄弟路径）",
    )
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="ingest --out-dir 目录")
    p.add_argument("--max-items", type=int, default=5, help="传入 ingest 的 --max-items")
    args = p.parse_args()

    direction = args.direction.strip()
    if not direction:
        _exit_ok(
            {
                "ok": False,
                "error": {
                    "code": "PREFLIGHT_EMPTY_DIRECTION",
                    "message": "direction 为空",
                    "hint": "请传入 --direction '…'",
                },
            }
        )

    finance_root: Path = args.finance_root.resolve()
    ingest_py = _resolve_ingest(finance_root)
    if not ingest_py.is_file():
        _exit_ok(
            {
                "ok": False,
                "error": {
                    "code": "PREFLIGHT_INGEST_NOT_FOUND",
                    "message": "未检测到外部信源技能（ingest.py 不存在）",
                    "hint": f"请确认 finance-source-ingest 与 streamy-content-gen 为兄弟目录，或传 --finance-root 指向该技能根目录。期望路径: {ingest_py}",
                },
            }
        )

    kw, domain_tags, kw_list = _expand_keywords(direction)
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    py_exe = _resolve_finance_venv_python(finance_root)

    cmd = [
        py_exe,
        str(ingest_py),
        "run",
        "--sources",
        "market,news,social",
        "--keywords",
        kw,
        "--max-items",
        str(max(1, int(args.max_items))),
        "--out-dir",
        str(out_dir),
    ]
    cwd = str((finance_root / "scripts").resolve())

    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _exit_ok(
            {
                "ok": False,
                "error": {
                    "code": "PREFLIGHT_INGEST_TIMEOUT",
                    "message": "finance-source-ingest 执行超时",
                    "hint": "请检查网络或 AkShare/东财可用性后重试",
                },
            }
        )
    except OSError as e:
        _exit_ok(
            {
                "ok": False,
                "error": {
                    "code": "PREFLIGHT_INGEST_OS_ERROR",
                    "message": str(e),
                    "hint": "无法启动子进程，请检查 Python 路径与权限",
                },
            }
        )

    if proc.returncode != 0:
        raw = (proc.stderr or proc.stdout or "").strip()
        if "Traceback" in raw or "Error" in raw:
            tail = "ingest 子进程报错（已省略 Python Traceback）；请检查 finance-source-ingest 的 venv、fetchers 与网络。"
        else:
            tail = raw.replace("\n", " ")[:800] if raw else "(无 stderr)"
        _exit_ok(
            {
                "ok": False,
                "error": {
                    "code": "PREFLIGHT_INGEST_FAILED",
                    "message": f"ingest 退出码 {proc.returncode}",
                    "hint": "请手动提供事实锚点或修复 finance-source-ingest 环境。" + tail,
                },
            }
        )

    snap_path = out_dir / "snapshot.json"
    if not snap_path.is_file():
        _exit_ok(
            {
                "ok": False,
                "error": {
                    "code": "PREFLIGHT_SNAPSHOT_MISSING",
                    "message": "ingest 成功但未找到 snapshot.json",
                    "hint": f"检查 --out-dir 与 ingest 写盘权限: {snap_path}",
                },
            }
        )

    try:
        snapshot = json.loads(snap_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _exit_ok(
            {
                "ok": False,
                "error": {
                    "code": "PREFLIGHT_SNAPSHOT_PARSE",
                    "message": str(e),
                    "hint": "snapshot.json 损坏或非 JSON，请重新运行 ingest",
                },
            }
        )

    hot_rank_py = _resolve_hot_rank_fetcher()
    hot_rank: dict[str, Any] = {"ok": False, "lists": [], "errors": [{"item": "hot_rank_script", "reason": "missing"}]}
    if hot_rank_py.is_file():
        last_reason = "unknown"
        for i in range(HOT_RANK_RETRY + 1):
            try:
                hr = subprocess.run(
                    [sys.executable, str(hot_rank_py), "--sites", "微博,抖音,百度,知乎", "--top", "10"],
                    cwd=str(SCRIPTS_DIR.resolve()),
                    capture_output=True,
                    text=True,
                    timeout=25 + i * 8,
                    check=False,
                )
                if hr.returncode == 0 and (hr.stdout or "").strip():
                    hot_rank = json.loads(hr.stdout)
                    if hot_rank.get("lists"):
                        break
                    last_reason = "empty_lists"
                else:
                    last_reason = (hr.stderr or hr.stdout or "").strip()[:300] or "nonzero_exit"
            except Exception as e:  # noqa: BLE001
                last_reason = f"{type(e).__name__}: {e}"
        if not hot_rank.get("lists"):
            hot_rank = {
                "ok": False,
                "lists": [],
                "errors": [{"item": "hot_rank_script", "reason": last_reason, "retry_count": HOT_RANK_RETRY}],
            }

    md_summary = str(snapshot.get("markdown_summary") or "")
    try:
        payload = _build_topic_payload(direction, snapshot, md_summary, hot_rank, domain_tags)
    except Exception as e:  # noqa: BLE001
        _exit_ok(
            {
                "ok": False,
                "error": {
                    "code": "PREFLIGHT_PAYLOAD_BUILD",
                    "message": str(e),
                    "hint": "请手动提供 source_context 与 evidence_anchor",
                },
            }
        )

    digest = _feishu_digest_bullets(md_summary)
    _exit_ok(
        {
            "ok": True,
            "topic_payload": payload,
            "feishu_digest_bullets": digest,
            "feishu_notice": payload.get("preflight_meta", {}).get("feishu_notice"),
            "feishu_source_notice": payload.get("preflight_meta", {}).get("feishu_source_notice"),
            "snapshot_path": str(snap_path),
            "ingest_keywords_used": kw,
            "ingest_keywords_expanded": kw_list,
            "domain_tags": domain_tags,
            "hint_ok": "将 topic_payload 作为唯一 JSON 体执行 draft_manager update --stage topic_picking 并落盘。飞书选题轮只回复：候选（标题+thesis+3 evidence）+ 选号指令；默认不展示信源状态/大盘/快讯摘要（除非用户显式要求回看来源）。禁止同一轮写大纲/逐字稿；选题确认后再 outline_refining",
        }
    )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — 禁止未捕获异常冒泡为 Traceback
        _emit(
            {
                "ok": False,
                "error": {
                    "code": "PREFLIGHT_UNEXPECTED",
                    "message": str(exc),
                    "hint": "请检查脚本版本或联系维护者；可改用手动构造 source_context 与 evidence_anchor",
                },
            }
        )
        sys.exit(0)
