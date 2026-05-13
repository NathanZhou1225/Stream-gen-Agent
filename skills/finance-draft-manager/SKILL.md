# finance-draft-manager（开稿素材层）v0.1.0

## 角色定位

**开稿素材检索器（Draft Layer）**。只读 SQLite DB，不联网，不抓取。
从 `finance-source-ingest` 沉淀的数据库"点菜"，组装 `source_context` / `evidence_pack`，
传给 `streamy-content-gen` 的 `draft_manager` 开稿门禁。

- **只做**：DB 检索 → Router 点菜 → 板块润色 → 输出上下文包；`db_snapshot.py` 从 DB 拼装 **飞书用 `markdown_summary`**
- **不做**：网络请求、数据入库（采集由 `finance-source-ingest` 负责）

如需与 **`ingest.py legacy` 实时 pipeline** 全文复杂度完全一致（北向/社媒情报嵌套等），请走 `query_market_facts.py --live-fetch`。

**DB 飞书快照（`scripts/db_snapshot.py`）**：默认 **`query_market_facts` 子进程**。六大板块可走 **`run_router` + 同池规则补位（含深度稿）+ 可选 `rewrite_sectors`**；**大事件** 独立长窗默认 **7 天**（`FINANCE_DB_SNAPSHOT_MAJOR_HOURS` / `--major-since-hours`）；**情绪量化** 对 DB 窗口新闻 / Router 结果 / 深度 / `sentiment_hot` 调用与 ingest 同源的 **`enhance_social_intelligence`**，写入 `sections.social_intelligence`，并读取 `social_intel_run_history` 中 **`legacy_pipeline` + `ingest_run`** 合并时间序列参与 FG（不写表）。开关：`FINANCE_DB_SNAPSHOT_USE_ROUTER`、`FINANCE_DB_SNAPSHOT_USE_REWRITE`（默认开）或 CLI `--no-router` / `--no-rewrite`。`router.py` / `rewriter.py` 的 OpenAI 兼容 URL 已自动补 `/v1/chat/completions`。

---

## 调用约定

### A. 构建开稿上下文（主路径）

```bash
python scripts/draft_retriever.py build-context \
  --direction "AI算力板块行情分析" \
  --since-hours 24 \
  [--db /path/to/finance_sources.db]
```

**stdout**：

```json
{
  "ok": true,
  "direction": "AI算力板块行情分析",
  "db_path": "...",
  "fetched_at": "2026-05-13T01:30:00+00:00",
  "source_context": [
    "- [科技|rsshub] 芯片涨停潮来袭：英伟达估值创历史新高：分析师预计 H100 供应缺口延续至 Q4",
    "..."
  ],
  "evidence_pack": {
    "direction": "AI算力板块行情分析",
    "core_facts": [...],
    "market_snapshot": [...],
    "source_gaps": [],
    "usage_hint": "先向用户展示本 evidence_pack；用户确认后再进入 user-style 选择/绑定。"
  }
}
```

### B. 仅检索条目（调试/预览）

```bash
python scripts/draft_retriever.py retrieve \
  --direction "新能源" --since-hours 24 --limit 10
```

### C. 查询 DB 状态

```bash
python scripts/draft_retriever.py status
```

---

## Router 与润色（LLM 驱动）

### LLM Router

`scripts/router.py` — 对检索结果按板块分组点菜，每板块最多选 3 条。

```python
from scripts.router import run_router
result = run_router(candidates)
# result.ids_by_sector -> {"科技": [1, 3], "新能源": [2]}
# result.insight_by_sector -> {"科技": "芯片供需缺口持续扩大"}
```

### 板块小 LLM 润色

`scripts/rewriter.py` — 对 Router 选出的条目重写展示文本（标题 / 影响 / 角度 / **sentiment**），默认关闭。JSON 每条须含 `sentiment`∈{利好,利空,中性}，并与正文结论一致；`db_snapshot` 渲染润色行时优先用该字段，**润色洞察**过短或占位时回退为同板块 Router 洞察。`db_snapshot` 对大事件 → 热点 → 深度资讯做跨节指纹去重，抑制同题反复占版。

```bash
FINANCE_SECTOR_LLM_REWRITE_ENABLED=1
```

---

## 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--direction` | 必填（build-context）| 自然语言方向，自动拆解为检索关键词 |
| `--since-hours` | `24` | 检索时间窗口（小时） |
| `--limit` | `10` | 返回最多条数 |
| `--sector` | 空 | 过滤板块（retrieve 命令） |
| `--db` | `user_data/finance_sources.db` | DB 路径 |

---

## 环境变量

| 变量 | 说明 |
|------|------|
| `FINANCE_DB_PATH` | DB 路径（覆盖默认） |
| `FINANCE_LLM_ROUTER_BASE_URL` | Router LLM 网关 |
| `FINANCE_LLM_ROUTER_API_KEY` | Router LLM 密钥 |
| `FINANCE_LLM_ROUTER_MODEL` | Router 模型 |
| `FINANCE_LLM_ROUTER_TIMEOUT_SEC` | 默认 `30` |
| `FINANCE_LLM_ROUTER_JSON_OBJECT` | 默认等价 **开**（设 `0`/`false` 关闭）；OpenAI 兼容 `response_format=json_object` |
| `FINANCE_LLM_ROUTER_MENU_PER_SECTOR` | **`db_snapshot`** 构建 Router 菜单时每板块最大条数（默认 `8`，过大时小模型易输出分析文字而非 JSON） |
| `FINANCE_LLM_ROUTER_MAX_TOKENS` / `FINANCE_LLM_ROUTER_TEMPERATURE` | Router 解码参数（默认 **`1200` / `0`**） |
| `FINANCE_SECTOR_LLM_REWRITE_ENABLED` | 默认 `0`（关闭），设 `1` 开启板块润色 |
| `FINANCE_SECTOR_LLM_JSON_OBJECT` | 默认等价 **开**（设 `0` 关闭） |
| `FINANCE_SECTOR_LLM_MAX_TOKENS` | 润色单次 `max_tokens`（默认 **`640`**） |
| `FINANCE_SECTOR_LLM_REWRITE_MAX_WORKERS` | 并发润色线程数（默认 **`2`**，减轻网关 burst） |
| `FINANCE_SECTOR_LLM_REWRITE_RETRY_EXTRA` | 失败后额外重试次数（默认 **`1`**，即最多 2 次请求；上限 2） |
| `FINANCE_SECTOR_LLM_REWRITE_RETRY_SLEEP_SEC` | 重试前间隔秒数（默认 **`0.4`**，设为 `0` 可关） |
| `FINANCE_SECTOR_LLM_BASE_URL` / `_API_KEY` / `_MODEL` | 可选；未设时回退 `FINANCE_LLM_ROUTER_*` → `FINANCE_INGEST_LLM_CLEAN_*` → Ark（见 `rewriter._load_config`） |

**说明**：`scripts/db_snapshot.py` 加载 `.env` 时，`workspace-stream-gen/.env` 中的键会**覆盖**已在 Shell/根环境中设置的同名变量，避免本地调试被旧值卡住。

---

## 目录结构

```
finance-draft-manager/
├── scripts/
│   ├── draft_retriever.py   # 主入口（retrieve / build-context / status）
│   ├── router.py            # LLM Router（从 pipeline.py 迁移）
│   └── rewriter.py          # 板块小 LLM 润色（从 pipeline.py 迁移）
└── SKILL.md
```

---

## 上下游依赖

- **上游**：`finance-source-ingest` 产生的 SQLite DB（不联网，DB 为唯一数据来源）
- **下游**：`streamy-content-gen` 的 `preflight_topic` → `draft_manager` evidence_pack 门禁

## 与 streamy-content-gen 的集成（P1 目标）

`preflight_topic.py` 增加 `--from-db` 参数后，调用链变为：
1. `preflight_topic --from-db --direction "..."` → 调 `draft_retriever.py build-context`
2. 拿到 `evidence_pack` → 进入 `draft_manager` 开稿门禁
3. 用户确认证据包后 → `user-style` 绑定 → 生成大纲/口播稿
