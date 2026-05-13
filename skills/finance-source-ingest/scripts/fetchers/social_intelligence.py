"""
社交媒体情报分析框架 · 量化增强模块
=============================================
核心思想移植自「社交媒体情报分析」SKILL，零LLM、纯规则、可集成：

1. 情绪打分系统（从三分类升级为连续分值）
2. 讨论热度量化（消息量、作者数、Z-score异常检测）
3. 恐惧贪婪指数（Fear & Greed Index）
4. 作者权重分级（机构/KOL/散户）
5. 平台权重聚合
6. 情绪反转信号检测

设计原则：
- 零API调用、纯本地计算
- 输入是标准化的 news/social 条目
- 输出是可直接入库的量化因子
"""

from __future__ import annotations

import math
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

# ============================================================
# 1. 情绪连续打分系统（从 [-1, 1]）
# ============================================================

# 权重化的情感关键词（分值代表权重）
SENTIMENT_WORDS: dict[str, float] = {
    # 强利好 (+2)
    "超预期": 2.0, "创历史新高": 2.0, "暴涨": 2.0, "大超预期": 2.0,
    "重大突破": 2.0, "里程碑": 2.0, "史诗级": 2.0, "引爆": 2.0,
    # 利好 (+1)
    "利好": 1.0, "上涨": 1.0, "突破": 1.0, "增长": 1.0, "盈利": 1.0,
    "回购": 1.0, "增持": 1.0, "签约": 1.0, "订单": 1.0, "合作": 1.0,
    "获批": 1.0, "通过": 1.0, "创新": 1.0, "领先": 1.0, "龙头": 1.0,
    "补贴": 1.0, "扶持": 1.0, "放宽": 1.0, "松绑": 1.0, "鼓励": 1.0,
    "支持": 1.0, "促进": 1.0, "涨价": 1.0, "提价": 1.0, "供不应求": 1.0,
    "降息": 1.0, "降准": 1.0, "宽松": 1.0, "刺激": 1.0, "净流入": 1.0,
    "大涨": 1.0, "强势": 1.0,
    # 强利空 (-2)
    "暴跌": -2.0, "崩盘": -2.0, "暴雷": -2.0, "违约": -2.0, "退市": -2.0,
    "闪崩": -2.0, "熔断": -2.0, "血崩": -2.0, "腰斩": -2.0,
    # 利空 (-1)
    "利空": -1.0, "下跌": -1.0, "亏损": -1.0, "减持": -1.0, "质押": -1.0,
    "冻结": -1.0, "查封": -1.0, "处罚": -1.0, "调查": -1.0, "立案": -1.0,
    "问询": -1.0, "诉讼": -1.0, "纠纷": -1.0, "制裁": -1.0, "打压": -1.0,
    "收紧": -1.0, "调控": -1.0, "降价": -1.0, "跌价": -1.0, "滞销": -1.0,
    "裁员": -1.0, "倒闭": -1.0, "破产": -1.0, "清算": -1.0, "事故": -1.0,
    "加息": -1.0, "缩表": -1.0, "紧缩": -1.0, "衰退": -1.0, "贸易战": -1.0,
    "净流出": -1.0, "大跌": -1.0, "跌停": -1.0, "走低": -1.0, "下调": -1.0,
}

# 强度修饰词（放大/缩小情绪）
INTENSIFIERS: dict[str, float] = {
    "大幅": 1.5, "显著": 1.5, "明显": 1.3, "强势": 1.5,
    "微弱": 0.5, "小幅": 0.5, "轻微": 0.5, "温和": 0.7,
    "超": 2.0, "史诗级": 2.5, "疯狂": 2.0, "极度": 2.0,
    "历史罕见": 2.5, "史上": 2.0, "创纪录": 2.0,
}

# 否定词（反转情绪）
NEGATORS: tuple[str, ...] = (
    "不", "没", "无", "未", "非", "否", "难", "难以", "并未", "尚未",
    "不会", "不可能", "无法", "没法", "不至于", "谈不上",
)


def sentiment_score(text: str) -> float:
    """计算文本的情绪连续分值，范围 [-1, 1]。

    算法：
    1. 统计所有情感词的加权和
    2. 应用修饰词和否定词的修正
    3. tanh 压缩到 [-1, 1] 区间

    Returns:
        -1.0 到 1.0 之间的分值，>0 利好，<0 利空，≈0 中性
    """
    score = 0.0
    matches = 0

    # 1. 基础情感词打分
    for word, weight in SENTIMENT_WORDS.items():
        count = text.count(word)
        if count > 0:
            score += weight * count
            matches += count

    # 2. 强度修饰词修正（简化：检测修饰词存在即整体放大）
    intensity_mult = 1.0
    for word, mult in INTENSIFIERS.items():
        if word in text:
            intensity_mult = max(intensity_mult, mult)
    score *= intensity_mult

    # 3. 否定词修正（简化：检测到否定即整体减半反转概率）
    neg_count = sum(1 for n in NEGATORS if n in text)
    if neg_count > 0 and neg_count % 2 == 1:
        score *= -0.5  # 否定打5折反转，避免过度纠正

    # 4. tanh 压缩到 [-1, 1]，保证极值有区分度
    if matches == 0:
        return 0.0
    return math.tanh(score / (matches * 0.8))  # 归一化


def sentiment_label(score: float) -> str:
    """分值转标签：>0.2 利好，<-0.2 利空，否则中性。"""
    if score > 0.2:
        return "利好"
    if score < -0.2:
        return "利空"
    return "中性"


# ============================================================
# 2. 讨论热度量化（Buzz Metrics）
# ============================================================

@dataclass
class BuzzMetrics:
    """单条目的热度量化指标。"""
    message_count: int = 1          # 消息数量
    unique_authors: int = 1         # 独立作者数（如果能识别）
    engagement_score: float = 0.0   # 互动分（点赞/转发/评论加权）
    buzz_zscore: float = 0.0        # 相对于历史的Z-score异常值
    topic_frequency: float = 0.0    # 话题频率（该话题/总消息量）


def compute_buzz_metrics(
    items: list[dict[str, Any]],
    window_size: int = 30,
) -> list[BuzzMetrics]:
    """批量计算一组条目的热度指标（与 ``items`` 顺序一一对应）。

    ``window_size`` 保留参数供未来接入多日历史窗口；当前批次内对
    ``engagement_score + topic_frequency`` 做样本标准差并写入 ``buzz_zscore``。
    """
    del window_size  # 当前实现为批次内 Z-score；多日历史接入时再使用

    topic_counts: dict[str, int] = defaultdict(int)
    for item in items:
        stocks = item.get("stock_mentions") or []
        for stock in stocks:
            topic_counts[stock] += 1

    total = len(items)
    buzz_row: list[BuzzMetrics] = []
    raw_values: list[float] = []

    for item in items:
        buzz = BuzzMetrics()
        hot_str = str(item.get("hot") or item.get("hotValue") or "0")
        try:
            hot_num = float(re.sub(r"[^\d.]", "", hot_str) or "0")
            buzz.engagement_score = math.log1p(hot_num) / 10
        except Exception:
            pass

        stocks = item.get("stock_mentions") or []
        if stocks and total > 0:
            avg_freq = sum(topic_counts[s] for s in stocks) / (len(stocks) * total)
            buzz.topic_frequency = min(avg_freq * 5, 1.0)

        raw = buzz.engagement_score + buzz.topic_frequency
        raw_values.append(raw)
        buzz_row.append(buzz)

    n = len(raw_values)
    if n < 2:
        return buzz_row

    mean = sum(raw_values) / n
    variance = sum((r - mean) ** 2 for r in raw_values) / (n - 1)
    std = math.sqrt(variance) if variance > 0 else 0.0
    for buzz, raw in zip(buzz_row, raw_values):
        buzz.buzz_zscore = (raw - mean) / std if std > 0 else 0.0

    return buzz_row


# ============================================================
# 3. 恐惧贪婪指数（Fear & Greed Index）
# ============================================================

def compute_fear_greed_index(
    sentiment_scores: list[float],
    buzz_scores: list[float],
    lookback_days: int = 30,
) -> float:
    """计算 0–100 的恐惧贪婪风格指数（**批次内分位秩**，非 CNN 官方成分）。

    在未接入多日外部历史时，语义为「当前 run 内条目集合上的相对位置」；
    可将历史序列通过 ``historical_sentiments`` / ``historical_buzz`` 拼入后再调用。

    档位可读性（与 CNN FGI 类比，非同源数据）：
    - 0-20: 极度恐惧
    - 20-40: 恐惧
    - 40-60: 中性
    - 60-80: 贪婪
    - 80-100: 极度贪婪

    Args:
        sentiment_scores: 情绪分值序列（[-1,1] 连续分），须与 buzz 对齐长度语义
        buzz_scores: 热度分值序列
        lookback_days: 参与分位计算的最大窗口（不超过序列长度）

    Returns:
        0-100 的恐惧贪婪指数
    """
    if not sentiment_scores or not buzz_scores:
        return 50.0  # 中性默认值

    # 使用最近的窗口数据计算百分位排名
    window = min(lookback_days, len(sentiment_scores), len(buzz_scores))
    recent_sentiment = sentiment_scores[-window:]
    recent_buzz = buzz_scores[-window:]

    # 1. 情绪分位数：当前情绪在历史中的排名
    current_sent = recent_sentiment[-1]
    sent_rank = sum(1 for s in recent_sentiment if s <= current_sent) / len(recent_sentiment)
    sent_score = sent_rank * 100

    # 2. 热度分位数：当前热度在历史中的排名
    current_buzz = recent_buzz[-1]
    buzz_rank = sum(1 for b in recent_buzz if b <= current_buzz) / len(recent_buzz)
    buzz_score = buzz_rank * 100

    # 3. 加权组合（情绪60% + 热度40%）
    fear_greed = 0.6 * sent_score + 0.4 * buzz_score

    return max(0.0, min(100.0, fear_greed))


def fear_greed_label(fg_index: float) -> tuple[str, str]:
    """恐惧贪婪指数转可读标签 + Emoji。"""
    if fg_index < 20:
        return "极度恐惧", "🔵"
    if fg_index < 40:
        return "恐惧", "🟦"
    if fg_index < 60:
        return "中性", "⬜"
    if fg_index < 80:
        return "贪婪", "🟨"
    return "极度贪婪", "🟥"


# ============================================================
# 4. 作者权重分级（机构/KOL/散户）
# ============================================================

AUTHOR_TYPE_PATTERNS: dict[str, tuple[str, float]] = {
    # 机构类（权重最高）
    "央行": ("institutional", 3.0),
    "证监会": ("institutional", 3.0),
    "新华社": ("institutional", 3.0),
    "人民日报": ("institutional", 3.0),
    "央视": ("institutional", 3.0),
    "财新": ("institutional", 2.5),
    "华尔街见闻": ("institutional", 2.5),
    "彭博": ("institutional", 2.5),
    "路透": ("institutional", 2.5),
    "券商": ("institutional", 2.0),
    "证券": ("institutional", 2.0),
    "研究所": ("institutional", 2.0),
    "中金": ("institutional", 2.5),
    "中信": ("institutional", 2.5),
    "华泰": ("institutional", 2.0),

    # KOL 类（权重次之）
    "巴菲特": ("kol", 2.5),
    "芒格": ("kol", 2.5),
    "达利欧": ("kol", 2.5),
    "但斌": ("kol", 2.0),
    "李大霄": ("kol", 1.5),

    # 官方媒体
    "官方": ("institutional", 2.0),
    "国务院": ("institutional", 3.0),
    "财政部": ("institutional", 3.0),
}


def classify_author(source: str, title: str = "") -> tuple[str, float]:
    """根据来源/标题判断作者类型和权重。

    Returns:
        (author_type: str, weight: float)
        author_type: "institutional" | "kol" | "retail" | "unknown"
        weight: 3.0(机构) → 2.0(KOL) → 1.0(散户)
    """
    text = f"{source} {title}"

    for pattern, (atype, weight) in AUTHOR_TYPE_PATTERNS.items():
        if pattern in text:
            return atype, weight

    # 默认散户
    return "retail", 1.0


# ============================================================
# 5. 平台权重聚合
# ============================================================

PLATFORM_WEIGHTS: dict[str, float] = {
    # 权威媒体（最高权重）
    "新华社": 0.35,
    "央视": 0.35,
    "人民日报": 0.35,
    "财新": 0.30,
    "华尔街见闻": 0.30,
    "彭博": 0.30,
    "路透": 0.30,

    # 券商研报（次高权重）
    "券商": 0.20,
    "证券": 0.20,

    # 财经资讯平台
    "财联社": 0.15,
    "新浪财经": 0.10,
    "东方财富": 0.10,

    # 社交媒体（最低权重，噪音多）
    "微博": 0.05,
    "雪球": 0.05,
    "股吧": 0.05,
    "reddit": 0.05,
    "twitter": 0.05,
}


def platform_weight_for_source(platform: str) -> float:
    """单来源字符串上取最大匹配权重（可命中多个 pattern，取 max）。"""
    weight = 0.05
    for pattern, w in PLATFORM_WEIGHTS.items():
        if pattern in platform:
            weight = max(weight, w)
    return weight


def aggregate_platform_sentiment(
    platform_scores: dict[str, float],
) -> float:
    """按平台权重聚合多来源的情绪分值。

    算法：加权平均，权威媒体权重高，社交媒体权重低
    """
    if not platform_scores:
        return 0.0

    total_weight = 0.0
    weighted_sum = 0.0

    for platform, score in platform_scores.items():
        w = platform_weight_for_source(platform)
        weighted_sum += score * w
        total_weight += w

    return weighted_sum / total_weight if total_weight > 0 else 0.0


# ============================================================
# 6. 情绪反转信号检测
# ============================================================

def detect_sentiment_reversal(
    fg_index_series: list[float],
    price_series: list[float] | None = None,
    extreme_greed_threshold: float = 80.0,
    extreme_fear_threshold: float = 20.0,
    confirmation_days: int = 3,
) -> dict[str, Any]:
    """检测情绪极端值后的反转信号。

    Args:
        fg_index_series: 恐惧贪婪指数的时间序列（最新在最后）
        price_series: 对应时间的价格序列（可选）
        extreme_greed_threshold: 极度贪婪阈值，默认80
        extreme_fear_threshold: 极度恐惧阈值，默认20
        confirmation_days: 需要连续多少天确认

    Returns:
        {
            "signal": 1(做空)/-1(做多)/0(无信号),
            "direction": "short"/"long"/"neutral",
            "strength": 信号强度 0-1,
            "reason": 信号说明
        }
    """
    if len(fg_index_series) < confirmation_days:
        return {"signal": 0, "direction": "neutral", "strength": 0.0, "reason": "数据不足"}

    recent = fg_index_series[-confirmation_days:]

    # 连续极度贪婪 → 做空信号（逃顶）
    if all(x >= extreme_greed_threshold for x in recent):
        avg_excess = (sum(recent) / len(recent) - extreme_greed_threshold) / 20
        return {
            "signal": 1,
            "direction": "short",
            "strength": min(avg_excess, 1.0),
            "reason": f"连续{confirmation_days}天极度贪婪，预期短期见顶回落",
        }

    # 连续极度恐惧 → 做多信号（抄底）
    if all(x <= extreme_fear_threshold for x in recent):
        avg_excess = (extreme_fear_threshold - sum(recent) / len(recent)) / 20
        return {
            "signal": -1,
            "direction": "long",
            "strength": min(avg_excess, 1.0),
            "reason": f"连续{confirmation_days}天极度恐惧，预期短期触底反弹",
        }

    return {"signal": 0, "direction": "neutral", "strength": 0.0, "reason": "无明显极端信号"}


# ============================================================
# 7. 主入口：统一增强处理
# ============================================================

def enhance_social_intelligence(
    items: list[dict[str, Any]],
    historical_sentiments: list[float] | None = None,
    historical_buzz: list[float] | None = None,
    historical_fg: list[float] | None = None,
    historical_run_sentiments: list[float] | None = None,
    historical_run_buzz: list[float] | None = None,
) -> dict[str, Any]:
    """为一批社交媒体/新闻条目注入完整的社交情报量化指标（**原地**写入条目 dict）。

    Args:
        items: 原始条目列表（需要包含 title/source 等字段）；函数会直接 ``item[...] =`` 写回
        historical_sentiments: 与 ``historical_buzz`` 同长度的**逐条**历史序列（拼入 FG 分位）
        historical_buzz: 逐条历史热度序列
        historical_fg: 历史恐惧贪婪指数序列（**仅**用于 ``detect_sentiment_reversal``，须为 0–100）
        historical_run_sentiments: 每次 **legacy/快照 run** 一条的 headline 情绪（与 DB 表对齐）
        historical_run_buzz: 与 run 对齐的池均 buzz；长度不足时用 0 垫齐

    当 ``historical_run_sentiments`` 非空时，FG 在「run 级序列 + 本 run 聚合点」上计算，
    ``fear_greed_scope`` 为 ``db_run_history``；否则沿用逐条 ``batch_relative`` 逻辑。
    """
    if not items:
        return {
            "enhanced_items": [],
            "aggregate_metrics": {},
            "stock_sentiments": {},
        }
    sentiment_scores: list[float] = []
    buzz_scores: list[float] = []

    for item in items:
        text = f"{item.get('title') or ''} {item.get('clean_text') or ''}"

        sent_score = sentiment_score(text)
        sent_label = sentiment_label(sent_score)
        item["sentiment_score"] = round(sent_score, 3)
        item["sentiment_label"] = sent_label
        sentiment_scores.append(sent_score)

        source = str(item.get("source") or item.get("platform") or "unknown")
        author_type, author_weight = classify_author(source, text)
        item["author_type"] = author_type
        item["author_weight"] = author_weight

        weighted_sent = sent_score * author_weight
        item["weighted_sentiment"] = round(weighted_sent, 3)

    buzz_row = compute_buzz_metrics(items)
    for item, buzz in zip(items, buzz_row):
        raw = buzz.engagement_score + buzz.topic_frequency
        item["buzz_score"] = round(raw, 3)
        item["buzz_zscore"] = round(buzz.buzz_zscore, 3)
        buzz_scores.append(item["buzz_score"])

    avg_sentiment = sum(sentiment_scores) / len(sentiment_scores) if sentiment_scores else 0.0

    src_accum: dict[str, list[float]] = defaultdict(list)
    for item in items:
        src = str(item.get("source") or item.get("platform") or "unknown")
        src_accum[src].append(float(item.get("sentiment_score", 0.0)))
    platform_avgs = {k: sum(v) / len(v) for k, v in src_accum.items()}
    platform_weighted = aggregate_platform_sentiment(platform_avgs)
    headline_sentiment = platform_weighted if platform_avgs else avg_sentiment

    mean_buzz_pool = statistics.mean(buzz_scores) if buzz_scores else 0.0

    hist_sent = historical_sentiments or []
    hist_buzz = historical_buzz or []
    run_hist_s = list(historical_run_sentiments or [])
    run_hist_b = list(historical_run_buzz or [])

    if len(run_hist_s) > 0:
        rb = run_hist_b
        if len(rb) < len(run_hist_s):
            rb = rb + [0.0] * (len(run_hist_s) - len(rb))
        elif len(rb) > len(run_hist_s):
            rb = rb[: len(run_hist_s)]
        fg_index = compute_fear_greed_index(
            run_hist_s + [headline_sentiment],
            rb + [mean_buzz_pool],
        )
        fear_greed_scope = "db_run_history"
    else:
        fg_index = compute_fear_greed_index(
            hist_sent + sentiment_scores,
            hist_buzz + buzz_scores,
        )
        fear_greed_scope = "batch_relative"

    fg_label, fg_emoji = fear_greed_label(fg_index)

    fg_series = list(historical_fg or []) + [fg_index]
    reversal = detect_sentiment_reversal(fg_series)

    buzz_zscore_peak = 0.0
    if items:
        buzz_zscore_peak = max(abs(float(it.get("buzz_zscore", 0.0))) for it in items)

    stock_sentiments: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "count": 0,
        "sentiment_sum": 0.0,
        "avg_sentiment": 0.0,
        "mentions": [],
    })

    for item in items:
        stocks = item.get("stock_mentions") or []
        sent = float(item.get("sentiment_score", 0.0))
        for stock in stocks:
            stock_sentiments[stock]["count"] += 1
            stock_sentiments[stock]["sentiment_sum"] += sent
            stock_sentiments[stock]["mentions"].append(item.get("title", ""))

    for stock, data in stock_sentiments.items():
        if data["count"] > 0:
            data["avg_sentiment"] = round(data["sentiment_sum"] / data["count"], 3)
            data["sentiment_label"] = sentiment_label(data["avg_sentiment"])

    return {
        "enhanced_items": items,
        "aggregate_metrics": {
            "avg_sentiment": round(avg_sentiment, 3),
            "platform_weighted_sentiment": round(platform_weighted, 3),
            "headline_sentiment": round(headline_sentiment, 3),
            "sentiment_label": sentiment_label(headline_sentiment),
            "fear_greed_index": round(fg_index, 1),
            "fear_greed_label": fg_label,
            "fear_greed_emoji": fg_emoji,
            "fear_greed_scope": fear_greed_scope,
            "total_items": len(items),
            "reversal_signal": reversal,
            "buzz_zscore_peak": round(buzz_zscore_peak, 3),
            "mean_buzz_score": round(mean_buzz_pool, 3),
        },
        "stock_sentiments": dict(stock_sentiments),
    }


# ============================================================
# 8. 便捷函数：单个条目增强
# ============================================================

def enhance_single_item(item: dict[str, Any]) -> dict[str, Any]:
    """增强单个条目，添加社交情报量化字段。"""
    result = enhance_social_intelligence([item])
    return result["enhanced_items"][0]


if __name__ == "__main__":
    # 简单测试
    test_items = [
        {
            "title": "央行宣布全面降准0.5个百分点，释放长期资金约1万亿元",
            "source": "新华社",
            "clean_text": "为支持实体经济发展，促进综合融资成本稳中有降，中国人民银行决定下调金融机构存款准备金率0.5个百分点。",
        },
        {
            "title": "英伟达暴跌10%，芯片板块集体回调",
            "source": "华尔街见闻",
            "clean_text": "受通胀数据超预期影响，美股芯片板块集体下跌，英伟达大跌10%，高通跌超11%。",
        },
        {
            "title": "某散户表示：现在就是抄底的好时机！",
            "source": "股吧",
            "clean_text": "我看现在市场情绪已经到了极度恐慌，正是进场抄底的好时候，梭哈！",
        },
    ]

    result = enhance_social_intelligence(test_items)

    print("=== 聚合指标 ===")
    for k, v in result["aggregate_metrics"].items():
        print(f"  {k}: {v}")

    print("\n=== 单条增强 ===")
    for item in result["enhanced_items"]:
        print(f"  标题: {item['title'][:40]}...")
        print(f"    情绪分: {item['sentiment_score']} ({item['sentiment_label']})")
        print(f"    作者类型: {item['author_type']} (权重: {item['author_weight']})")
        print(f"    加权情绪分: {item['weighted_sentiment']}")
        print(f"    热度分: {item['buzz_score']}")
        print()

    print("=== 股票情绪汇总 ===")
    for stock, data in result["stock_sentiments"].items():
        print(f"  {stock}: {data['avg_sentiment']} ({data['sentiment_label']}) - {data['count']}条提及")
