"""规则 based 情感分析引擎（移植自 finance-news-pro · A 股场景强化）。

零 LLM，纯关键词匹配，符合「主链工具脚本不调 API」约束。
"""

from __future__ import annotations

from typing import Any

# ——— 利好关键词库 ——————————————————————————————————————————————————————

POSITIVE_KEYWORDS: tuple[str, ...] = (
    # 基础利好
    "利好", "上涨", "突破", "超预期", "增长", "盈利", "收益", "分红",
    "回购", "增持", "签约", "订单", "合作", "获批", "通过", "获奖",
    "创新", "领先", "龙头", "稀缺", "独家", "专利",
    "补贴", "扶持", "放宽", "松绑", "鼓励", "支持", "促进",
    "重组", "并购", "注入", "分拆", "上市", "IPO", "定增", "配股",
    "涨价", "提价", "供不应求", "扩张", "投产", "达产",
    "技术突破", "产品发布", "新品", "迭代", "产品升级", "效率提升",
    # 宏观利好
    "降息", "降准", "宽松", "刺激", "减税", "宽信用",
    "稳增长", "加大支持", "积极财政",
    # 资金利好
    "净流入", "大幅流入", "持续流入", "北向净买",
    # 其他正面
    "创历史新高", "大涨", "强势", "暴涨",
)

# ——— 利空关键词库 ——————————————————————————————————————————————————————

NEGATIVE_KEYWORDS: tuple[str, ...] = (
    # 基础利空
    "利空", "下跌", "暴跌", "崩盘", "亏损", "暴雷", "违约", "退市",
    "减持", "质押", "冻结", "查封", "处罚", "调查", "立案", "问询",
    "诉讼", "纠纷", "仲裁", "赔偿", "罚款", "警告", "谴责", "黑名单",
    "限制", "禁令", "制裁", "打压", "收紧", "调控", "限购", "限产",
    "降价", "跌价", "滞销", "库存", "积压", "产能过剩",
    "裁员", "倒闭", "破产", "清算", "重组失败", "终止", "取消", "延期",
    "事故", "爆炸", "泄漏", "污染", "召回", "缺陷",
    # 宏观利空
    "加息", "缩表", "紧缩", "通胀超预期", "衰退",
    "贸易战", "贸易制裁", "出口管制", "实体清单", "关税加征", "加征关税",
    # 资金利空
    "净流出", "大幅流出", "持续流出", "北向净卖",
    # 其他负面
    "暴跌", "大跌", "跌停", "崩", "闪崩",
)

# 明确负面强信号：命中即优先判为利空，避免被正面词抵消
STRONG_NEGATIVE_KEYWORDS: tuple[str, ...] = (
    "跌超",
    "走低",
    "下调",
    "减持",
    "退市",
    "亏损",
    "立案",
)

# ——— 影响评估关键词库 ——————————————————————————————————————————————————

MARKET_LEVEL_KEYWORDS: tuple[str, ...] = (
    "央行", "美联储", "财政部", "国务院", "中共中央",
    "GDP", "CPI", "PPI", "PMI", "失业率",
    "降准", "降息", "加息", "缩表", "宽松", "紧缩",
    "战争", "冲突", "地缘", "制裁", "贸易战",
    "全球市场", "系统性", "货币政策", "财政政策",
    "金融危机", "主权", "国际结算",
)

SECTOR_LEVEL_KEYWORDS: tuple[str, ...] = (
    "行业", "板块", "产业", "赛道",
    "新能源", "芯片", "半导体", "科技股", "金融板块", "地产", "消费",
    "医疗", "医药", "有色金属", "黄金", "银行业", "保险", "券商",
    "光伏", "储能", "锂电", "AI板块", "算力", "大模型",
    "互联网", "云计算", "军工", "化工",
)

# ——— 个股映射表 ——————————————————————————————————————————————————————

STOCK_MAPPING: dict[str, dict[str, str]] = {
    # A 股 · 科技/算力
    "宁德时代": {"code": "300750.SZ", "market": "A", "sector": "新能源"},
    "比亚迪": {"code": "002594.SZ", "market": "A", "sector": "新能源"},
    "贵州茅台": {"code": "600519.SH", "market": "A", "sector": "消费"},
    "五粮液": {"code": "000858.SZ", "market": "A", "sector": "消费"},
    "招商银行": {"code": "600036.SH", "market": "A", "sector": "金融"},
    "工商银行": {"code": "601398.SH", "market": "A", "sector": "金融"},
    "平安银行": {"code": "000001.SZ", "market": "A", "sector": "金融"},
    "中国平安": {"code": "601318.SH", "market": "A", "sector": "金融"},
    "中信证券": {"code": "600030.SH", "market": "A", "sector": "金融"},
    "东方财富": {"code": "300059.SZ", "market": "A", "sector": "金融"},
    "隆基绿能": {"code": "601012.SH", "market": "A", "sector": "新能源"},
    "阳光电源": {"code": "300274.SZ", "market": "A", "sector": "新能源"},
    "迈瑞医疗": {"code": "300760.SZ", "market": "A", "sector": "医疗"},
    "恒瑞医药": {"code": "600276.SH", "market": "A", "sector": "医疗"},
    "药明康德": {"code": "603259.SH", "market": "A", "sector": "医疗"},
    "中芯国际": {"code": "688981.SH", "market": "A", "sector": "芯片"},
    "海康威视": {"code": "002415.SZ", "market": "A", "sector": "科技"},
    "立讯精密": {"code": "002475.SZ", "market": "A", "sector": "科技"},
    "工业富联": {"code": "601138.SH", "market": "A", "sector": "科技"},
    "中科曙光": {"code": "603019.SH", "market": "A", "sector": "算力"},
    "浪潮信息": {"code": "000977.SZ", "market": "A", "sector": "算力"},
    "天孚通信": {"code": "300394.SZ", "market": "A", "sector": "科技"},
    "中际旭创": {"code": "300308.SZ", "market": "A", "sector": "科技"},
    "科大讯飞": {"code": "002230.SZ", "market": "A", "sector": "AI"},
    "华为": {"code": "N/A", "market": "A", "sector": "科技"},
    # 港股
    "腾讯": {"code": "0700.HK", "market": "HK", "sector": "科技"},
    "腾讯控股": {"code": "0700.HK", "market": "HK", "sector": "科技"},
    "阿里巴巴": {"code": "9988.HK", "market": "HK", "sector": "科技"},
    "美团": {"code": "3690.HK", "market": "HK", "sector": "科技"},
    "小米": {"code": "1810.HK", "market": "HK", "sector": "科技"},
    "小米集团": {"code": "1810.HK", "market": "HK", "sector": "科技"},
    "京东": {"code": "9618.HK", "market": "HK", "sector": "科技"},
    "百度": {"code": "9888.HK", "market": "HK", "sector": "科技"},
    "网易": {"code": "9999.HK", "market": "HK", "sector": "科技"},
    "快手": {"code": "1024.HK", "market": "HK", "sector": "科技"},
    "比亚迪股份": {"code": "1211.HK", "market": "HK", "sector": "新能源"},
    "理想汽车": {"code": "2015.HK", "market": "HK", "sector": "新能源"},
    "小鹏汽车": {"code": "9868.HK", "market": "HK", "sector": "新能源"},
    "蔚来": {"code": "9866.HK", "market": "HK", "sector": "新能源"},
    "汇丰控股": {"code": "0005.HK", "market": "HK", "sector": "金融"},
    "友邦保险": {"code": "1299.HK", "market": "HK", "sector": "金融"},
    "港交所": {"code": "0388.HK", "market": "HK", "sector": "金融"},
    # 美股
    "苹果": {"code": "AAPL", "market": "US", "sector": "科技"},
    "微软": {"code": "MSFT", "market": "US", "sector": "科技"},
    "谷歌": {"code": "GOOGL", "market": "US", "sector": "科技"},
    "亚马逊": {"code": "AMZN", "market": "US", "sector": "科技"},
    "英伟达": {"code": "NVDA", "market": "US", "sector": "芯片"},
    "特斯拉": {"code": "TSLA", "market": "US", "sector": "新能源"},
    "Meta": {"code": "META", "market": "US", "sector": "科技"},
    "英特尔": {"code": "INTC", "market": "US", "sector": "芯片"},
    "AMD": {"code": "AMD", "market": "US", "sector": "芯片"},
    "高通": {"code": "QCOM", "market": "US", "sector": "芯片"},
    "博通": {"code": "AVGO", "market": "US", "sector": "芯片"},
    "台积电": {"code": "TSM", "market": "US", "sector": "芯片"},
    "高盛": {"code": "GS", "market": "US", "sector": "金融"},
    "摩根士丹利": {"code": "MS", "market": "US", "sector": "金融"},
    "摩根大通": {"code": "JPM", "market": "US", "sector": "金融"},
    "花旗": {"code": "C", "market": "US", "sector": "金融"},
    "巴菲特": {"code": "BRK.A", "market": "US", "sector": "金融"},
    "伯克希尔": {"code": "BRK.A", "market": "US", "sector": "金融"},
}


# ——— 核心分类函数 ——————————————————————————————————————————————————————

def classify_sentiment(text: str) -> str:
    """返回 '利好' | '利空' | '中性'（规则 based，零 LLM）。

    正负各计命中数：净正 > 0 → 利好；净负 > 0 → 利空；平局或均无命中 → 中性。
    单个明确信号（如"降准"）在无负向词时即可判定，避免过严漏报。
    """
    if any(kw in text for kw in STRONG_NEGATIVE_KEYWORDS):
        return "利空"
    pos = sum(1 for kw in POSITIVE_KEYWORDS if kw in text)
    neg = sum(1 for kw in NEGATIVE_KEYWORDS if kw in text)
    if pos > neg:
        return "利好"
    if neg > pos:
        return "利空"
    return "中性"


def classify_impact(text: str) -> str:
    """返回 '市场' | '行业' | '公司'（规则 based）。

    优先判定市场级（宏观/货币政策/地缘），其次行业级，否则公司级。
    """
    if any(kw in text for kw in MARKET_LEVEL_KEYWORDS):
        return "市场"
    if any(kw in text for kw in SECTOR_LEVEL_KEYWORDS):
        return "行业"
    return "公司"


def extract_stock_mentions(text: str) -> list[str]:
    """从文本中提取已知股票/公司名称列表（顺序稳定）。"""
    return [name for name in STOCK_MAPPING if name in text]


def sentiment_emoji(sentiment: str) -> str:
    """情感标签 → Emoji 标记。'利好'→🟢 '利空'→🔴 '中性'→⚪"""
    return {"利好": "🟢", "利空": "🔴", "中性": "⚪"}.get(sentiment, "⚪")


def enrich_item(item: dict[str, Any]) -> dict[str, Any]:
    """非破坏性地为新闻条目补充情感/影响/股票字段（已有字段不覆盖）。

    读取 title + clean_text + summary 拼合文本后分类。
    返回新 dict，原 item 不变。
    """
    text = " ".join(filter(None, [
        str(item.get("title") or ""),
        str(item.get("clean_text") or ""),
        str(item.get("summary") or ""),
    ]))
    result = dict(item)
    if "sentiment_hint" not in result:
        s = classify_sentiment(text)
        result["sentiment_hint"] = s
        result["sentiment_emoji"] = sentiment_emoji(s)
    if "impact_level" not in result:
        result["impact_level"] = classify_impact(text)
    if "stock_mentions" not in result:
        result["stock_mentions"] = extract_stock_mentions(text)
    return result
