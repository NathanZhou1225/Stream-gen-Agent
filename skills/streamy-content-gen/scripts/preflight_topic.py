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
import os
import platform
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from snapshot_cache import (
    DEFAULT_CACHE_SNAPSHOT as SNAPSHOT_CACHE_DEFAULT,
    DEFAULT_MAX_AGE_HOURS,
    try_load_fresh_snapshot,
    write_snapshot_cache,
)

# streamy-content-gen 根目录（本文件位于 scripts/）
SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = SKILL_ROOT.parent.parent  # workspace-stream-gen/
# 与 finance-source-ingest 为同 skills/ 下兄弟目录（可迁移：仅依赖此相对布局）
DEFAULT_FINANCE_SIBLING = SKILL_ROOT.parent / "finance-source-ingest"
# 默认写入缓存路径，与定时 cron 生成的快照对齐，避免重复生成
DEFAULT_OUT_DIR = WORKSPACE_ROOT / "cache" / "snapshot"
PROVENANCE = "finance-source-ingest|preflight_topic.py"
# markdown 用于候选抽取的软上限（降上下文体积，减少 topic 阶段耗时）
MAX_MD_CHARS = 1600
# 注入 source_context 的事实短摘条数（避免把整段 markdown 喂给后续阶段）
SOURCE_CONTEXT_BULLET_CAP = 8
# 飞书/对话可见的「热点摘要」条数（与 SKILL 契约一致，控制篇幅）
FEISHU_DIGEST_MAX = 8
FEISHU_DIGEST_LINE_CHARS = 220
# 拉取信息后、选题前可追问的单条详情上限
DETAIL_OPTIONS_MAX = 6
DETAIL_SUMMARY_CHARS = 220
DETAIL_TEXT_CHARS = 900
EVIDENCE_PACK_DETAIL_MAX = 5
HOTLIST_DOWN_NOTICE = "⚠️ 弱舆情热榜信号暂不可用（两路来源均失败），本轮选题基于行情/快讯/公告生成。"
CST = timezone(timedelta(hours=8))


def _env_int(name: str, default: int, *, min_v: int, max_v: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        v = int(raw) if raw else default
    except ValueError:
        v = default
    return max(min_v, min(max_v, v))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# 热榜默认关闭（Sprint：省 preflight 5–15s）；设 PREFLIGHT_SKIP_HOT_RANK=0 可恢复。
PREFLIGHT_SKIP_HOT_RANK_DEFAULT = True

# 热榜默认快速失败，避免 preflight 在弱网场景下卡 60-100s。
# 如需增强热榜鲁棒性，可通过环境变量调高重试与超时。
HOT_RANK_RETRY = _env_int("PREFLIGHT_HOT_RANK_RETRY", 0, min_v=0, max_v=3)
HOT_RANK_TIMEOUT_SEC = _env_int("PREFLIGHT_HOT_RANK_TIMEOUT_SEC", 8, min_v=4, max_v=30)
HOT_RANK_TIMEOUT_STEP_SEC = _env_int("PREFLIGHT_HOT_RANK_TIMEOUT_STEP_SEC", 4, min_v=0, max_v=15)

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


def _run_cloud_query_market_facts(
    *,
    out_dir: Path,
    cache_path: Path,
    keywords: str,
    timeout_sec: int = 180,
) -> None:
    """调用 query_market_facts（云端 API，--full）并写入 snapshot.json。"""
    qmf = SCRIPTS_DIR / "query_market_facts.py"
    cmd = [
        sys.executable,
        str(qmf),
        "--sources",
        "market,news,social",
        "--full",
        "--force-refresh",
    ]
    if keywords.strip():
        cmd.extend(["--keywords", keywords.strip()])
    proc = subprocess.run(
        cmd,
        cwd=str(WORKSPACE_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_sec,
        check=False,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-800:]
        raise RuntimeError(f"query_market_facts 退出码 {proc.returncode}: {tail}")
    snapshot = json.loads(proc.stdout)
    out_dir.mkdir(parents=True, exist_ok=True)
    snap_path = out_dir / "snapshot.json"
    snap_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")


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
    # digest 元信息行（易误当「快讯」排到候选①）
    if any(
        k in s
        for k in (
            "指数源",
            "📡",
            "行业强弱",
            "主力净流入",
            "市场热词",
            "涨跌停统计",
        )
    ):
        return True
    if any(
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
        )
    ):
        return True
    # 仅把「涨跌停统计/家数」类打底行当指数流，避免把「一字涨停」等公司快讯误踢出 news
    if "涨跌停统计" in s or re.search(r"涨停\s*\d+\s*家", s) or re.search(r"跌停\s*\d+\s*家", s):
        return True
    return False


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


def _strict_direction_hints(direction: str) -> tuple[str, ...] | None:
    """用户方向强约束词（用于从全量 bullets 前插同域行，避免 domain_enhanced 落在列表末尾抢不到候选①）。"""
    d = direction or ""
    if any(k in d for k in ("美伊", "伊朗", "中东", "地缘", "霍尔木兹", "以伊", "以色列")):
        return (
            "美伊", "伊朗", "中东", "地缘", "霍尔木兹", "以色列", "以伊", "特朗普", "袭击",
            "制裁", "原油", "石油", "避险", "冲突", "战争",
        )
    if any(k in d for k in ("黄金", "有色", "贵金属", "COMEX")):
        return ("黄金", "有色", "贵金属", "COMEX", "白银", "现货金", "能源金属", "铜", "铝", "锌", "锂", "镍")
    if any(k in d for k in ("新能源", "光伏", "风电", "储能", "锂电", "电池", "充电桩")):
        return DOMAIN_LEXICON.get("energy_new", ("新能源", "光伏", "风电", "储能", "锂电", "电池", "充电桩"))
    return None


def _prefer_domain_news_order(news: list[str], direction: str) -> list[str]:
    """按用户方向把同域快讯排到前面，避免候选①落在通用 digest 行上。"""
    hints = _strict_direction_hints(direction)
    if not hints:
        return news
    touched = [b for b in news if any(h in b for h in hints)]
    if not touched:
        return news
    rest = [b for b in news if b not in touched]
    return touched + rest


def _promote_hint_bullets_first(
    news: list[str],
    raw_bullets: list[str],
    hints: tuple[str, ...],
) -> list[str]:
    """在全量 raw_bullets 中找出命中 hints 的叙事行，整组前插到 news（保留去重顺序）。"""
    if not hints or not raw_bullets:
        return news
    promoted: list[str] = []
    seen: set[str] = set()
    for b in raw_bullets:
        if _is_noise_bullet(b) or _is_placeholder_line(b):
            continue
        if not any(h in b for h in hints):
            continue
        if _is_index_or_flow_bullet(b):
            continue
        key = _norm_title_key(b)
        if key in seen:
            continue
        seen.add(key)
        promoted.append(b)
    if not promoted:
        return news
    rest = [b for b in news if _norm_title_key(b) not in seen]
    return promoted + rest


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
    news = _prefer_domain_news_order(news, direction)
    dh = _strict_direction_hints(direction)
    if dh:
        news = _promote_hint_bullets_first(news, raw_bullets, dh)
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


def _clip_text(text: str, max_len: int) -> str:
    s = re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1].rstrip("，、；;:： ") + "…"


def _item_body_text(item: dict[str, Any]) -> str:
    for key in ("clean_text", "summary", "detail", "content", "description", "title"):
        val = str(item.get(key) or "").strip()
        if val:
            return val
    return ""


def _item_source_ref(item: dict[str, Any], default_source: str) -> str:
    src = str(item.get("source_name") or item.get("platform") or default_source or "").strip()
    ts = str(item.get("published_at") or item.get("time") or "").strip()
    if src and ts:
        return f"{src} {ts[:19].replace('T', ' ')}"
    return src or ts or default_source or "ingest_snapshot"


def _public_detail_option(option: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in option.items() if not k.startswith("_") and v not in (None, "")}


def _detail_options_from_snapshot(snapshot: dict[str, Any], md_summary: str) -> list[dict[str, Any]]:
    """
    从同一份 ingest snapshot 抽取可追问详情的条目。

    只返回已有信源字段，不联网、不补造；后续 `--detail-id` 会用同样规则复算并取回单条详情。
    """
    sections = snapshot.get("sections") or {}
    pools: list[tuple[str, str, list[dict[str, Any]]]] = []

    llm_router = sections.get("llm_router") or {}
    by_sector = llm_router.get("items_by_sector") or {}
    if isinstance(by_sector, dict):
        for sec, rows in by_sector.items():
            if isinstance(rows, list):
                pools.append((f"llm_router.{sec}", str(sec), rows))

    news = sections.get("news") or {}
    news_by_sector = news.get("items_by_sector") or {}
    if isinstance(news_by_sector, dict):
        for sec, rows in news_by_sector.items():
            if isinstance(rows, list):
                pools.append((f"news.{sec}", str(sec), rows))
    if isinstance(news.get("items_other_flash"), list):
        pools.append(("news.other_flash", "今日热点", news.get("items_other_flash") or []))

    for source_path, label in (
        ("deep_news.items", "深度内容"),
        ("global_macro.items", "大事件"),
        ("macro_hot.items", "热点"),
        ("social.items", "社媒"),
    ):
        head, tail = source_path.split(".", 1)
        sec = sections.get(head) or {}
        rows = sec.get(tail) or []
        if isinstance(rows, list):
            pools.append((source_path, label, rows))

    options: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_option(
        *,
        title: str,
        body: str,
        source_type: str,
        source_ref: str,
        source_path: str,
        sector: str | None = None,
        raw_line: str | None = None,
    ) -> None:
        title_clean = _clip_text(title or body, 88)
        body_clean = _clip_text(body or title, DETAIL_TEXT_CHARS)
        if not title_clean or not body_clean:
            return
        key = _norm_title_key(f"{title_clean}{body_clean[:40]}")
        if not key or key in seen:
            return
        seen.add(key)
        detail_id = f"D{len(options) + 1}"
        options.append(
            {
                "detail_id": detail_id,
                "title": title_clean,
                "summary": _clip_text(body_clean, DETAIL_SUMMARY_CHARS),
                "source_type": source_type,
                "source_ref": source_ref,
                "source_path": source_path,
                "sector": sector,
                "_detail_text": body_clean,
                "_raw_line": raw_line,
            }
        )

    for source_path, label, rows in pools:
        for item in rows:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            body = _item_body_text(item)
            if not title and not body:
                continue
            add_option(
                title=title or body,
                body=body or title,
                source_type=label,
                source_ref=_item_source_ref(item, label),
                source_path=source_path,
                sector=label if label in DOMAIN_LEXICON.get("teacher_focus", ()) else None,
            )
            if len(options) >= DETAIL_OPTIONS_MAX:
                return options

    for line in _markdown_bullet_lines(md_summary, cap=DETAIL_OPTIONS_MAX * 2):
        if _is_noise_bullet(line) or _is_placeholder_line(line):
            continue
        add_option(
            title=_title_from_bullet_line(line, max_len=88),
            body=line[2:].strip() if line.strip().startswith("- ") else line.strip(),
            source_type=_source_type_from_line(line),
            source_ref=_source_ref_from_line(line),
            source_path="markdown_summary",
            raw_line=line,
        )
        if len(options) >= DETAIL_OPTIONS_MAX:
            break

    return options


def _build_detail_payload(snapshot: dict[str, Any], detail_id: str) -> dict[str, Any]:
    md_summary = str(snapshot.get("markdown_summary") or "")
    options = _detail_options_from_snapshot(snapshot, md_summary)
    wanted = (detail_id or "").strip().upper()
    for opt in options:
        if str(opt.get("detail_id") or "").upper() != wanted:
            continue
        public = _public_detail_option(opt)
        detail_text = str(opt.get("_detail_text") or opt.get("summary") or "").strip()
        return {
            **public,
            "detail_text": detail_text,
            "usage_hint": "将本条详情展示给用户后，再询问是否进入选题与风格选择；不得基于该详情直接越级生成大纲/逐字稿。",
        }
    raise ValueError(f"detail_id 不存在：{detail_id}；可用值为 {[x.get('detail_id') for x in options]}")


def _candidate_index_from_arg(candidate_id: str, total: int) -> int:
    raw = (candidate_id or "").strip().upper()
    if raw.startswith("C"):
        raw = raw[1:]
    try:
        n = int(raw)
    except ValueError as e:
        raise ValueError(f"candidate_id 必须是 1~{total} 或 C1/C2/C3；当前={candidate_id!r}") from e
    if n < 1 or n > total:
        raise ValueError(f"candidate_id 越界：当前候选共 {total} 条（合法 1~{total}）")
    return n


def _evidence_pack_units(text: str) -> set[str]:
    s = re.sub(r"\s+", "", text or "")
    units: set[str] = set()
    for token in re.split(r"[，。、；;:：!！?？|｜/\-—\s]+", text or ""):
        token = token.strip()
        if len(token) >= 2:
            units.add(token[:12])
    for words in DOMAIN_LEXICON.values():
        for word in words:
            if word and word in s:
                units.add(word)
    # 中文无空格时，用短 n-gram 保底，避免“油价冲击验证时刻”整句匹配不到。
    compact = re.sub(r"[^\w\u4e00-\u9fff]+", "", s)
    for n in (2, 3):
        for i in range(max(0, len(compact) - n + 1)):
            piece = compact[i : i + n]
            if len(piece) == n:
                units.add(piece)
            if len(units) > 80:
                break
    return {x for x in units if len(x) >= 2}


def _candidate_match_text(candidate: dict[str, Any], direction: str) -> str:
    parts = [
        direction,
        str(candidate.get("title") or ""),
        str(candidate.get("thesis") or ""),
        str(candidate.get("angle") or ""),
        str(candidate.get("evidence_anchor") or ""),
    ]
    ev = candidate.get("evidence")
    if isinstance(ev, list):
        for row in ev:
            if isinstance(row, dict):
                parts.append(str(row.get("point") or ""))
                parts.append(str(row.get("source_ref") or ""))
    return " ".join(parts)


def _detail_match_score(option: dict[str, Any], units: set[str]) -> int:
    text = " ".join(
        str(option.get(k) or "")
        for k in ("title", "summary", "source_type", "source_ref", "sector", "_detail_text", "_raw_line")
    )
    if not text or not units:
        return 0
    return sum(1 for u in units if u and u in text)


def _evidence_rows_from_details(details: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for opt in details:
        title = str(opt.get("title") or opt.get("summary") or "").strip()
        if not title:
            continue
        stype = str(opt.get("source_type") or "snapshot_detail")
        rows.append(
            {
                "point": _clip_text(title, 88),
                "source_type": stype,
                "source_ref": str(opt.get("source_ref") or opt.get("source_path") or "ingest_snapshot"),
                "confidence": _confidence_for_source(stype),
            }
        )
    return rows[:3]


def _run_targeted_detail_fetch(
    *,
    finance_root: Path,
    out_dir: Path,
    query: str,
    max_items: int,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    ingest_py = _resolve_ingest(finance_root)
    if not ingest_py.is_file():
        return None, {"ok": False, "reason": f"ingest.py missing: {ingest_py}"}
    target_dir = out_dir / "candidate_evidence"
    target_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        _resolve_finance_venv_python(finance_root),
        str(ingest_py),
        "legacy",
        "--sources",
        "market,news,social",
        "--keywords",
        _keywords_from_direction(query),
        "--max-items",
        str(max(1, int(max_items))),
        "--out-dir",
        str(target_dir),
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str((finance_root / "scripts").resolve()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
    except Exception as e:  # noqa: BLE001
        return None, {"ok": False, "reason": f"{type(e).__name__}: {e}"}
    if proc.returncode != 0:
        return None, {"ok": False, "reason": (proc.stderr or proc.stdout or "nonzero_exit").strip()[:500]}
    snap_path = target_dir / "snapshot.json"
    try:
        snapshot = json.loads(snap_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return None, {"ok": False, "reason": f"snapshot_parse_failed: {type(e).__name__}: {e}"}
    return snapshot, {"ok": True, "snapshot_path": str(snap_path), "keywords": _keywords_from_direction(query)}


def _build_candidate_evidence_pack(
    *,
    topic_payload: dict[str, Any],
    snapshot: dict[str, Any],
    candidate_id: str,
    finance_root: Path,
    out_dir: Path,
    allow_targeted_fetch: bool,
    max_items: int,
) -> dict[str, Any]:
    candidates = topic_payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("topic_payload.candidates 为空或不是数组")
    n = _candidate_index_from_arg(candidate_id, len(candidates))
    candidate = candidates[n - 1]
    if not isinstance(candidate, dict):
        raise ValueError(f"candidates[{n}] 不是 JSON object")

    direction = str(topic_payload.get("direction") or (topic_payload.get("preflight_meta") or {}).get("direction") or "")
    match_text = _candidate_match_text(candidate, direction)
    units = _evidence_pack_units(match_text)
    md_summary = str(snapshot.get("markdown_summary") or "")
    details = _detail_options_from_snapshot(snapshot, md_summary)
    scored = sorted(
        [(opt, _detail_match_score(opt, units)) for opt in details],
        key=lambda x: x[1],
        reverse=True,
    )
    matched = [opt for opt, score in scored if score > 0][:EVIDENCE_PACK_DETAIL_MAX]
    targeted_fetch: dict[str, Any] = {"attempted": False, "ok": False}

    if len(matched) < 2 and allow_targeted_fetch:
        query = " ".join(
            x
            for x in (
                direction,
                str(candidate.get("title") or ""),
                str(candidate.get("thesis") or ""),
            )
            if x
        )
        fetched_snapshot, fetch_meta = _run_targeted_detail_fetch(
            finance_root=finance_root,
            out_dir=out_dir,
            query=query,
            max_items=max_items,
        )
        targeted_fetch = {"attempted": True, **fetch_meta}
        if fetched_snapshot:
            fetched_details = _detail_options_from_snapshot(
                fetched_snapshot,
                str(fetched_snapshot.get("markdown_summary") or ""),
            )
            fetched_scored = sorted(
                [(opt, _detail_match_score(opt, units)) for opt in fetched_details],
                key=lambda x: x[1],
                reverse=True,
            )
            for opt, score in fetched_scored:
                if score <= 0:
                    continue
                key = _norm_title_key(str(opt.get("title") or opt.get("summary") or ""))
                if any(_norm_title_key(str(x.get("title") or x.get("summary") or "")) == key for x in matched):
                    continue
                opt = dict(opt)
                opt["source_path"] = f"targeted_fetch:{opt.get('source_path')}"
                matched.append(opt)
                if len(matched) >= EVIDENCE_PACK_DETAIL_MAX:
                    break

    public_details = [_public_detail_option(x) for x in matched[:EVIDENCE_PACK_DETAIL_MAX]]
    candidate_evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), list) else []
    detail_rows = _evidence_rows_from_details(matched)
    source_gaps = []
    if len(public_details) < 2:
        source_gaps.append("当前 snapshot 与一次定向补充中，能强匹配该候选方向的详情不足 2 条；大纲应只使用已列证据，避免补造。")

    return {
        "candidate_id": f"C{n}",
        "candidate_index": n,
        "candidate_title": str(candidate.get("title") or ""),
        "candidate_thesis": str(candidate.get("thesis") or ""),
        "direction": direction,
        "core_facts": [
            row
            for row in candidate_evidence
            if isinstance(row, dict)
        ][:3],
        "detailed_sources": public_details,
        "argument_boosters": detail_rows,
        "source_gaps": source_gaps,
        "targeted_fetch": targeted_fetch,
        "usage_hint": "先向用户展示本 evidence_pack；用户确认后再进入 user-style 选择/绑定。不得跳过证据包直接生成大纲。",
    }


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


def validate_candidate_choice(
    topic_payload: dict[str, Any],
    candidate_id: str,
    *,
    min_score: float = 0.10,
) -> dict[str, Any]:
    """校验用户所选候选与 direction 的关联度（供 evidence_pack / workflow helper 复用）。"""
    direction = str(
        topic_payload.get("direction")
        or (topic_payload.get("preflight_meta") or {}).get("direction")
        or ""
    ).strip()
    candidates = topic_payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("topic_payload.candidates 为空或不是数组")
    n = _candidate_index_from_arg(candidate_id, len(candidates))
    candidate = candidates[n - 1]
    if not isinstance(candidate, dict):
        raise ValueError(f"candidates[{n}] 不是 JSON object")
    score = _candidate_relevance(candidate, direction)
    scores = [_candidate_relevance(c, direction) for c in candidates if isinstance(c, dict)]
    best_idx = max(range(len(scores)), key=lambda i: scores[i]) + 1 if scores else n
    ok = score >= min_score
    return {
        "ok": ok,
        "candidate_index": n,
        "candidate_id": f"C{n}",
        "relevance_score": round(score, 4),
        "min_score": min_score,
        "direction": direction,
        "candidate_title": str(candidate.get("title") or ""),
        "all_relevance_scores": [round(s, 4) for s in scores],
        "best_candidate_index": best_idx,
        "hint": (
            None
            if ok
            else (
                f"候选 {n} 与方向关联度 {score:.2f} 低于阈值 {min_score}；"
                f"建议改选候选 {best_idx} 或补充更具体的开稿方向。"
            )
        ),
    }


def _attach_snapshot_meta(
    topic_payload: dict[str, Any],
    *,
    snapshot_path: Path,
    snapshot_cached: bool,
    snapshot_fetched_at: str | None,
) -> None:
    pfm = topic_payload.setdefault("preflight_meta", {})
    if not isinstance(pfm, dict):
        return
    pfm["snapshot_path"] = str(snapshot_path.resolve())
    pfm["snapshot_cached"] = snapshot_cached
    if snapshot_fetched_at:
        pfm["snapshot_fetched_at"] = snapshot_fetched_at


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
    if hot_rank.get("skipped"):
        signal_secondary = _pick_social_signal(snapshot)
        return (
            {
                "primary_source": "disabled",
                "secondary_source": "social_api(vvhan->akshare)",
                "fallback_used": bool(signal_secondary),
                "fallback_reason": "hot_rank_skipped",
                "skipped": True,
            },
            signal_secondary,
            None,
        )
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
    finance_root: Path = DEFAULT_FINANCE_SIBLING,
    out_dir: Path | None = None,
    allow_targeted_fetch: bool = False,
    max_items: int = 5,
    skip_evidence_precompute: bool = False,
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

    # ── 证据包预计算（合并 optim：preflight 一次性产出所有候选证据包，无需二次 --candidate-id 调用）──
    candidate_evidence_packs: dict[str, dict[str, Any]] = {}
    evidence_pack_precompute_errors: list[dict[str, Any]] = []
    if not skip_evidence_precompute:
        topic_payload_for_ep: dict[str, Any] = {
            "direction": direction.strip(),
            "candidates": candidates,
        }
        _ep_out = out_dir or DEFAULT_OUT_DIR
        for _i, _cand in enumerate(candidates, start=1):
            try:
                ep = _build_candidate_evidence_pack(
                    topic_payload=topic_payload_for_ep,
                    snapshot=snapshot,
                    candidate_id=str(_i),
                    finance_root=finance_root,
                    out_dir=_ep_out,
                    allow_targeted_fetch=allow_targeted_fetch,
                    max_items=max_items,
                )
                candidate_evidence_packs[str(_i)] = ep
            except Exception as _exc:
                evidence_pack_precompute_errors.append({"candidate_index": _i, "error": str(_exc)})

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
            "candidate_evidence_pack_required": True,
            "evidence_pack_instruction": "证据包已与 topic_payload 预计算合并；用户选定候选后直接从 candidate_evidence_packs[索引] 提取展示，无需二次调用 preflight_topic.py --candidate-id。",
            "evidence_pack_precomputed": not skip_evidence_precompute,
            "evidence_pack_precompute_errors": evidence_pack_precompute_errors if evidence_pack_precompute_errors else None,
        },
        "candidate_evidence_packs": candidate_evidence_packs if candidate_evidence_packs else None,
    }


def main() -> None:
    t0_all = time.perf_counter()
    timing: dict[str, float] = {}

    p = argparse.ArgumentParser(description="带方向开稿：拉取 finance-source-ingest 事实并输出 topic_payload JSON")
    p.add_argument("--direction", help="用户自然语言选题方向")
    p.add_argument(
        "--finance-root",
        type=Path,
        default=DEFAULT_FINANCE_SIBLING,
        help="finance-source-ingest 技能根目录（默认同 workspace 兄弟路径）",
    )
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="ingest --out-dir 目录")
    p.add_argument("--max-items", type=int, default=5, help="传入 ingest 的 --max-items")
    p.add_argument("--snapshot-path", type=Path, help="读取已有 snapshot.json（详情模式可用）")
    p.add_argument("--detail-id", help="详情模式：从 snapshot 中读取指定 detail_id（如 D1）")
    p.add_argument("--topic-payload-file", type=Path, help="证据包模式：读取上轮 topic_payload JSON 文件")
    p.add_argument("--candidate-id", help="证据包模式：候选方向编号（1/2/3 或 C1/C2/C3）")
    p.add_argument("--allow-targeted-fetch", action="store_true", help="证据包不足时允许按候选方向做一次定向补充拉取")
    p.add_argument(
        "--source-mode",
        choices=["cloud", "legacy", "db"],
        default="cloud",
        help="数据源：cloud（默认，云端 API）/ legacy（实时抓取，需网络）；db 已废弃为 cloud 别名",
    )
    p.add_argument(
        "--snapshot-max-age-hours",
        type=int,
        default=6,
        help="快照缓存最大过期时间（小时），默认6小时",
    )
    p.add_argument(
        "--cache-snapshot-path",
        type=Path,
        help="快照缓存路径（优先读取，过期则重新生成）",
    )
    p.add_argument(
        "--no-hot-rank",
        action="store_true",
        help="跳过热榜 fetch_hot_rank（与 PREFLIGHT_SKIP_HOT_RANK=1 等效）",
    )
    args = p.parse_args()

    if args.candidate_id:
        snap_path = (args.snapshot_path or (args.out_dir / "snapshot.json")).resolve()
        topic_payload_path = args.topic_payload_file.resolve() if args.topic_payload_file else None
        if not snap_path.is_file():
            _exit_ok(
                {
                    "ok": False,
                    "error": {
                        "code": "PREFLIGHT_EVIDENCE_SNAPSHOT_MISSING",
                        "message": "证据包模式未找到 snapshot.json",
                        "hint": f"请传 --snapshot-path 指向 preflight 上轮返回的 snapshot_path。当前路径: {snap_path}",
                    },
                }
            )
        if topic_payload_path is None or not topic_payload_path.is_file():
            _exit_ok(
                {
                    "ok": False,
                    "error": {
                        "code": "PREFLIGHT_EVIDENCE_TOPIC_PAYLOAD_MISSING",
                        "message": "证据包模式未找到 topic_payload JSON",
                        "hint": "请将上轮返回的 topic_payload 保存为 JSON，并用 --topic-payload-file 指向它。",
                    },
                }
            )
        try:
            snapshot = json.loads(snap_path.read_text(encoding="utf-8"))
            topic_payload = json.loads(topic_payload_path.read_text(encoding="utf-8"))
            choice_check = validate_candidate_choice(topic_payload, str(args.candidate_id))
            if not choice_check.get("ok"):
                _exit_ok(
                    {
                        "ok": False,
                        "error": {
                            "code": "PREFLIGHT_CANDIDATE_LOW_RELEVANCE",
                            "message": choice_check.get("hint") or "所选候选与开稿方向关联度不足",
                            "hint": "请改选其他候选或补充更具体的 --direction 后重新 preflight。",
                        },
                        "choice_validation": choice_check,
                    }
                )
            evidence_pack = _build_candidate_evidence_pack(
                topic_payload=topic_payload,
                snapshot=snapshot,
                candidate_id=str(args.candidate_id),
                finance_root=args.finance_root.resolve(),
                out_dir=args.out_dir,
                allow_targeted_fetch=bool(args.allow_targeted_fetch),
                max_items=max(1, int(args.max_items)),
            )
        except (OSError, json.JSONDecodeError, ValueError) as e:
            _exit_ok(
                {
                    "ok": False,
                    "error": {
                        "code": "PREFLIGHT_EVIDENCE_PACK_FAILED",
                        "message": str(e),
                        "hint": "请确认 candidate_id 来自上一轮候选编号，topic_payload_file 与 snapshot_path 属于同一轮 preflight。",
                    },
                }
            )
        _exit_ok(
            {
                "ok": True,
                "mode": "candidate_evidence_pack",
                "evidence_pack": evidence_pack,
                "snapshot_path": str(snap_path),
                "topic_payload_file": str(topic_payload_path),
                "choice_validation": choice_check,
                "hint_ok": "先展示 evidence_pack；用户确认后再进入 user-style 选择/绑定，不得同轮越级输出大纲/逐字稿。",
            }
        )

    if args.detail_id:
        snap_path = (args.snapshot_path or (args.out_dir / "snapshot.json")).resolve()
        if not snap_path.is_file():
            _exit_ok(
                {
                    "ok": False,
                    "error": {
                        "code": "PREFLIGHT_DETAIL_SNAPSHOT_MISSING",
                        "message": "详情模式未找到 snapshot.json",
                        "hint": f"请传 --snapshot-path 指向 preflight 上轮返回的 snapshot_path。当前路径: {snap_path}",
                    },
                }
            )
        try:
            snapshot = json.loads(snap_path.read_text(encoding="utf-8"))
            detail_payload = _build_detail_payload(snapshot, str(args.detail_id))
            detail_options = [
                _public_detail_option(x)
                for x in _detail_options_from_snapshot(snapshot, str(snapshot.get("markdown_summary") or ""))
            ]
        except (OSError, json.JSONDecodeError, ValueError) as e:
            _exit_ok(
                {
                    "ok": False,
                    "error": {
                        "code": "PREFLIGHT_DETAIL_BUILD_FAILED",
                        "message": str(e),
                        "hint": "请确认 detail_id 来自上一轮 preflight 返回的 detail_options[]，且 snapshot_path 未被覆盖。",
                    },
                }
            )
        _exit_ok(
            {
                "ok": True,
                "mode": "detail",
                "detail_payload": detail_payload,
                "detail_options": detail_options,
                "snapshot_path": str(snap_path),
                "hint_ok": "先展示 detail_payload；随后询问用户是否进入选题与风格选择，不得同轮越级输出大纲/逐字稿。",
            }
        )

    direction = (args.direction or "").strip()
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
    kw, domain_tags, kw_list = _expand_keywords(direction)
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    source_mode = getattr(args, "source_mode", "cloud")
    if source_mode == "db":
        source_mode = "cloud"
    snapshot_max_age = getattr(args, "snapshot_max_age_hours", 6)
    cache_snapshot_path = getattr(args, "cache_snapshot_path", None)
    
    # ── 快照缓存路径优先级 ──────────────────────────────────────────────
    # 1. --cache-snapshot-path 显式指定
    # 2. workspace 标准缓存路径 cache/snapshot/snapshot.json
    if cache_snapshot_path is None:
        cache_snapshot_path = SNAPSHOT_CACHE_DEFAULT
    
    # ── 数据源：默认 cloud（API），legacy 显式联网 ─────────────────────────────
    t_ingest_start = time.perf_counter()
    cached_snapshot_used = False
    cached_snapshot_fetched_at = None
    cache_load_info: dict[str, Any] = {}

    if source_mode == "cloud":
        cached_snapshot, cache_load_info = try_load_fresh_snapshot(
            cache_snapshot_path,
            max_age_hours=snapshot_max_age,
            check_remote_db=True,
        )
        if cached_snapshot is not None:
            snapshot = cached_snapshot
            cached_snapshot_used = True
            cached_snapshot_fetched_at = cache_load_info.get("snapshot_fetched_at")
            snap_path = cache_snapshot_path
            timing["cached_snapshot_sec"] = round(time.perf_counter() - t_ingest_start, 3)
        else:
            try:
                _run_cloud_query_market_facts(
                    out_dir=out_dir,
                    cache_path=cache_snapshot_path,
                    keywords=kw,
                    timeout_sec=180,
                )
            except subprocess.TimeoutExpired:
                _exit_ok(
                    {
                        "ok": False,
                        "error": {
                            "code": "PREFLIGHT_CLOUD_SNAPSHOT_TIMEOUT",
                            "message": "query_market_facts 执行超时",
                            "hint": "请检查 FINANCE_CLOUD_API_* 与 finance-ingest-cloud API/Worker",
                        },
                    }
                )
            except (OSError, json.JSONDecodeError, RuntimeError) as e:
                _exit_ok(
                    {
                        "ok": False,
                        "error": {
                            "code": "PREFLIGHT_CLOUD_SNAPSHOT_FAILED",
                            "message": str(e)[:500],
                            "hint": "云端 Newsbox 不可用；可显式 --source-mode legacy（需网络与 RSS 配置）",
                        },
                    }
                )

    else:
        # legacy 路径：实时抓取（--source-mode legacy 显式触发）
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

        py_exe = _resolve_finance_venv_python(finance_root)
        _cmd = [
            py_exe,
            str(ingest_py),
            "legacy",
            "--sources", "market,news,social",
            "--keywords", kw,
            "--max-items", str(max(1, int(args.max_items))),
            "--out-dir", str(out_dir),
        ]
        try:
            _proc = subprocess.run(
                _cmd,
                cwd=str((finance_root / "scripts").resolve()),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
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

        if _proc.returncode != 0:
            raw = (_proc.stderr or _proc.stdout or "").strip()
            tail = "ingest 子进程报错；请检查 finance-source-ingest 的 venv、fetchers 与网络。" \
                if ("Traceback" in raw or "Error" in raw) \
                else (raw.replace("\n", " ")[:800] or "(无 stderr)")
            _exit_ok(
                {
                    "ok": False,
                    "error": {
                        "code": "PREFLIGHT_INGEST_FAILED",
                        "message": f"ingest 退出码 {_proc.returncode}",
                        "hint": "请手动提供事实锚点或修复 finance-source-ingest 环境。" + tail,
                    },
                }
            )

    timing["ingest_sec"] = round(time.perf_counter() - t_ingest_start, 3)

    # 快照路径优先级：
    # 1. 已复用缓存快照（cached_snapshot_used）
    # 2. 新生成的快照（out_dir/snapshot.json）
    if cached_snapshot_used:
        snap_path = cache_snapshot_path
        # snapshot 已在前面读取
    else:
        snap_path = out_dir / "snapshot.json"
        if not snap_path.is_file():
            _exit_ok(
                {
                    "ok": False,
                    "error": {
                        "code": "PREFLIGHT_SNAPSHOT_MISSING",
                        "message": "未找到 snapshot.json",
                        "hint": f"检查 --out-dir 与写盘权限: {snap_path}",
                    },
                }
            )

        t_snapshot_parse_start = time.perf_counter()
        try:
            snapshot = json.loads(snap_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            _exit_ok(
                {
                    "ok": False,
                    "error": {
                        "code": "PREFLIGHT_SNAPSHOT_PARSE",
                        "message": str(e),
                        "hint": "snapshot.json 损坏或非 JSON，请重新运行",
                    },
                }
            )
        timing["snapshot_parse_sec"] = round(time.perf_counter() - t_snapshot_parse_start, 3)

    skip_hot_rank = bool(getattr(args, "no_hot_rank", False)) or _env_bool(
        "PREFLIGHT_SKIP_HOT_RANK",
        default=PREFLIGHT_SKIP_HOT_RANK_DEFAULT,
    )
    hot_rank: dict[str, Any] = {"ok": False, "lists": [], "errors": [{"item": "hot_rank_script", "reason": "missing"}]}
    t_hot_rank_start = time.perf_counter()
    if skip_hot_rank:
        hot_rank = {"ok": False, "lists": [], "skipped": True, "skip_reason": "PREFLIGHT_SKIP_HOT_RANK"}
    else:
        hot_rank_py = _resolve_hot_rank_fetcher()
        if hot_rank_py.is_file():
            last_reason = "unknown"
            for i in range(HOT_RANK_RETRY + 1):
                try:
                    hr = subprocess.run(
                        [sys.executable, str(hot_rank_py), "--sites", "微博,抖音,百度,知乎", "--top", "10"],
                        cwd=str(SCRIPTS_DIR.resolve()),
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=HOT_RANK_TIMEOUT_SEC + i * HOT_RANK_TIMEOUT_STEP_SEC,
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
    timing["hot_rank_sec"] = round(time.perf_counter() - t_hot_rank_start, 3)
    timing["hot_rank_skipped"] = skip_hot_rank

    md_summary = str(snapshot.get("markdown_summary") or "")
    t_payload_start = time.perf_counter()
    try:
        payload = _build_topic_payload(
            direction,
            snapshot,
            md_summary,
            hot_rank,
            domain_tags,
            finance_root=finance_root,
            out_dir=out_dir,
            allow_targeted_fetch=getattr(args, "allow_targeted_fetch", False),
            max_items=max(1, int(args.max_items)),
        )
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
    timing["build_payload_sec"] = round(time.perf_counter() - t_payload_start, 3)
    timing["total_sec"] = round(time.perf_counter() - t0_all, 3)

    _attach_snapshot_meta(
        payload,
        snapshot_path=snap_path,
        snapshot_cached=cached_snapshot_used,
        snapshot_fetched_at=(
            cached_snapshot_fetched_at
            if cached_snapshot_used
            else (snapshot.get("meta") or {}).get("fetched_at")
        ),
    )

    digest = _feishu_digest_bullets(md_summary)
    
    # 构建快照来源提示
    snapshot_source_hint = ""
    if cached_snapshot_used:
        snapshot_source_hint = f"📊 快照来源：缓存复用（数据截止时间：{cached_snapshot_fetched_at or '未知'}，节省约 {timing.get('cached_snapshot_sec', 0)}s）"
    else:
        snapshot_fetched_at = (snapshot.get("meta") or {}).get("fetched_at")
        snapshot_source_hint = f"📊 快照来源：新生成（数据截止时间：{snapshot_fetched_at or '未知'}，耗时 {timing.get('ingest_sec', 0)}s）"
    
    _exit_ok(
        {
            "ok": True,
            "topic_payload": payload,
            "feishu_digest_bullets": digest,
            "evidence_pack_instruction": "证据包已预计算并内嵌于 topic_payload.candidate_evidence_packs；用户选定候选后直接从 payload 提取对应证据包展示，无需二次调用 preflight_topic。",
            "feishu_notice": payload.get("preflight_meta", {}).get("feishu_notice"),
            "feishu_source_notice": payload.get("preflight_meta", {}).get("feishu_source_notice"),
            "snapshot_path": str(snap_path),
            "snapshot_cached": cached_snapshot_used,
            "snapshot_fetched_at": cached_snapshot_fetched_at if cached_snapshot_used else (snapshot.get("meta") or {}).get("fetched_at"),
            "snapshot_source_hint": snapshot_source_hint,
            "ingest_keywords_used": kw,
            "ingest_keywords_expanded": kw_list,
            "domain_tags": domain_tags,
            "preflight_timing": timing,
            "hint_ok": "将 topic_payload 作为唯一 JSON 体执行 draft_manager update --stage topic_picking 并展示三候选。用户选择候选后，从 candidate_evidence_packs 提取并展示该方向证据包，再进入 user-style 选择/绑定。禁止同一轮写大纲/逐字稿。",
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
