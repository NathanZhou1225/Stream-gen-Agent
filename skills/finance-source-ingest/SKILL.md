# finance-source-ingest（Finance Newsbox 采集层）v0.2.2

## 角色定位

**金融信息采集箱（Ingest Layer）**。与外部数据源对抗，执行抓取、LLM 清洗、入库，是整个内容流水线的"数据水源"。

- **只做**：抓取 → 标准化 → raw 入库 → LLM 清洗 → prune → stdout 极简 JSON
- **不做**：选题、观点生成、Router、markdown_summary（已迁移到 `finance-draft-manager`）

---

## 调用约定

### A. 标准入库（v0.2.2 新路径）

```bash
python scripts/ingest.py run \
  --sources market,news,social \
  --keywords "AI 算力" \
  --max-items 30 \
  --prune-days 7
```

**stdout**（极简 JSON，供程序解析）：

```json
{
  "ok": true,
  "inserted": 25,
  "updated": 3,
  "cleaned": 18,
  "pruned": 0,
  "started_at": "2026-05-13T01:30:00+00:00",
  "finished_at": "2026-05-13T01:30:18+00:00",
  "db_path": "/root/.openclaw/workspace-stream-gen/user_data/finance_sources.db"
}
```

加 `--preview` 附加最近 5 条新闻标题（调试用）。

### B. 旧路径兼容（给 `query_market_facts.py` / `preflight_topic.py` 使用）

```bash
python scripts/ingest.py legacy --sources market,news,social --keywords "AI"
```

输出完整 `snapshot.json` + `markdown_summary`，与旧版行为一致。

### C. 修复 RSSHub

```bash
python scripts/ingest.py repair-rsshub --decision confirm
```

---

## 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--sources` | `market,news,social` | 逗号分隔，可用值: `market` `news` `social` `macro` `deep` `policy` `all` |
| `--keywords` | 空 | 关键词过滤（空格分隔），传给各 Collector |
| `--max-items` | `30` | 每个 Collector 最大条目数 |
| `--prune-days` | `7` | 清理多少天前的数据（每次 run 自动执行） |
| `--db` | `user_data/finance_sources.db` | DB 路径（可用 `FINANCE_DB_PATH` 环境变量覆盖） |
| `--no-clean` | 关闭 | 跳过 LLM 清洗（fast mode） |
| `--preview` | 关闭 | 附加最近 5 条新闻供调试 |

---

## 环境变量

| 变量 | 说明 |
|------|------|
| `FINANCE_INGEST_LLM_CLEAN_ENABLED` | 默认 `1`（开启 LLM 清洗）；设 `0` 关闭 |
| `FINANCE_INGEST_LLM_CLEAN_MODEL` | 清洗模型，空时回退 `OPENCLAW_ARK_MODEL` |
| `FINANCE_INGEST_LLM_CLEAN_BASE_URL` | 清洗网关，空时回退 `OPENCLAW_ARK_BASE_URL` |
| `FINANCE_INGEST_LLM_CLEAN_API_KEY` | 清洗密钥，空时回退 `OPENCLAW_ARK_API_KEY` |
| `FINANCE_INGEST_LLM_CLEAN_TIMEOUT_SEC` | 默认 `25`（单次 HTTP 读超时；仍报错可再加到 `45`） |
| `FINANCE_INGEST_LLM_CLEAN_MAX_TOKENS` | 默认 `1024`（补全 max_tokens，避免中文 JSON+tags 被截断导致解析失败） |
| `FINANCE_INGEST_LLM_CLEAN_BATCH_SIZE` | 默认 `10`（每批从库中取几条；每条各调用 1 次 LLM） |
| `FINANCE_INGEST_LLM_CLEAN_MAX_ROUNDS_PER_RUN` | 默认 `0` = 不限制批次数，当次 `run`/`clean` 一直洗到无 `pending`；设为**正数**（如 `30`）则每轮最多洗这么多批，适合 cron 控制单次耗时，剩余 `pending` 由下次定时任务继续 |
| `FINANCE_DB_PATH` | DB 文件路径（覆盖默认） |
| `FINANCE_SOCIAL_INTEL_HISTORY_ENABLED` | 默认 `1`：读表 `social_intel_run_history` 最近 N 次 run（**合并** `legacy_pipeline` + `ingest_run`），参与 FG 与反转；`0` 关闭（`ingest.py run` 收尾也不再 append） |
| `FINANCE_SOCIAL_INTEL_HISTORY_RUNS` | 默认 `30`，最多读入的历史 run 条数 |
| `FINANCE_SOCIAL_INTEL_HISTORY_APPEND` | 默认 `1`：仅 **legacy `build_snapshot`** 成功后在 DB **追加**一行（`source_kind=legacy_pipeline`）；`0` 不写 |
| `FINANCE_SOCIAL_INTEL_INGEST_APPEND` | 默认 `1`：**`ingest.py run`** 收尾对「本窗新闻」算聚合并 append（`source_kind=ingest_run`）；`0` 关闭 |
| `FINANCE_SOCIAL_INTEL_INGEST_LOOKBACK_MINUTES` | 默认 `120`，与 ``run.started_at`` 取较早者作为 ``fetched_at`` 下限，扩大本窗 |
| `FINANCE_SOCIAL_INTEL_INGEST_MAX_NEWS` | 默认 `500`，本窗最多取多少条新闻参与计算 |

**Base URL**：填 `https://api.deepseek.com` 或 `https://api.deepseek.com/v1` 均可（脚本会自动拼到 `/v1/chat/completions`）。

### 仅补洗积压（不抓取）

```bash
python scripts/ingest.py clean
# 或限制本命令最多 20 批：python scripts/ingest.py clean --max-rounds 20
# 将 failed 改回 pending 再全量重试：python scripts/ingest.py clean --retry-failed
```

**每次 `run` 入库后，会自动分批洗完所有 `pending` 吗？**

- **默认**（`FINANCE_INGEST_LLM_CLEAN_MAX_ROUNDS_PER_RUN` 未设或设为 **`0`**）：**会**。`ingest.py run` 在本次执行内会**多轮**取 `pending` → 调 LLM → 写回，直到库里没有 `pending`（单条失败会进 `failed`，不再占 `pending`）。
- **若为正整数**（如 `30`）：**单次** `run`（或 `clean`）**最多**洗这么多**批**；每批条数 = `FINANCE_INGEST_LLM_CLEAN_BATCH_SIZE`。剩余的 `pending` 留给**下一次**定时任务或你再执行一次 `ingest.py clean`。**「慢慢洗」**：把该变量设小 + 依赖多次 cron / 手写多跑几次即可。

你只看到 **2 条** `clean_*` 有值，通常是因为之前只跑过**带限制的补洗**（例如测试时 `MAX_ROUNDS=1` 且 `BATCH_SIZE=2`）。要对当前积压**全部洗完**：在项目根执行 **`python scripts/ingest.py clean`**（保持默认 `MAX_ROUNDS=0`），或跑一次 **`ingest.py run`**（不要加 `--no-clean`）；耗时 ≈ `pending 条数 × 单次 LLM 耗时`（串行逐条调用），条数多时可跑十几分钟以上。

---

## 目录结构

```
finance-source-ingest/
├── collectors/           # 采集器（Newsbox 模式，各信源独立）
│   ├── base.py           # BaseCollector 抽象类
│   ├── sina_market.py    # 新浪三大指数 + 北向
│   ├── sina_live.py      # 新浪7x24宏观快讯
│   ├── cls_telegraph.py  # 财联社电报
│   ├── rsshub.py         # RSSHub 六大板块
│   ├── deep_news.py      # 直连深度资讯
│   ├── policy_gov.py     # 监管公告
│   └── social_hot.py     # 社媒热搜
├── models/               # Pydantic-like dataclass 数据契约
│   ├── item.py           # RawNewsItem / CleanedFields
│   ├── market.py         # MarketSnapshot
│   ├── sentiment.py      # SentimentHotItem
│   └── run.py            # IngestRun
├── fetchers/             # P0 兼容层（P1 清除，collectors 内部使用）
├── storage.py            # SQLite init/upsert/prune/query
├── cleaner.py            # LLM 清洗层（默认开启，失败不阻断）
├── setup_cron.sh         # 安装/移除 cron 定时任务
└── scripts/
    └── ingest.py         # 统一入口（run / clean / legacy / init-db / prune / repair-rsshub）
```

---

## 定时采集（cron）

```bash
./setup_cron.sh --dry-run    # 预览三条 cron 条目
./setup_cron.sh --install    # 写入 crontab（周一~五 09:00/12:00/17:00）
./setup_cron.sh --remove     # 移除
```

---

## 数据库位置

`workspace-stream-gen/user_data/finance_sources.db`（可 `FINANCE_DB_PATH` 覆盖）

### 核心表

| 表 | 说明 |
|----|------|
| `news_items` | 新闻/快讯，含 raw 与 clean 双层字段 |
| `market_snapshots` | 大盘指数快照 |
| `sentiment_hot` | 社媒热搜情绪 |
| `ingest_runs` | 每次 `ingest.py run` 结束瞬间的统计快照（**见下文**，与逐条清洗状态不是同一张「真相表」） |
| `social_intel_run_history` | 每次快照级一行：`source_kind` 为 **`legacy_pipeline`**（legacy `build_snapshot`）或 **`ingest_run`**（`ingest.py run` 收尾）；读历史时**两种合并**参与 FG。与 `news_items` 等同按 ``prune_old(days)`` 清理（见 `storage.prune_old`） |
| `source_state` | 各信源最后抓取状态 |

### 清洗状态以哪张表为准？

**以 `news_items` 为准**，看列 **`llm_clean_status`**：

| 取值 | 含义 |
|------|------|
| `pending` | 已入库，**尚未**成功跑完 LLM 清洗（或正在排队等待下几批） |
| `done` | 已成功写回 `clean_title` / `clean_summary` / `sector` 等清洗字段 |
| `failed` | 该条清洗调用失败（如超时、API 错误），**raw 仍在**；可修了配置后重试（需后续「重置 pending」能力或手工更新，当前以新入库条目为主） |

图形化工具里若只展开 **`ingest_runs`**，看到 **`cleaned = 0`**：**不代表**库里每一条都是脏数据——那只是**那一场 run 结束时**，统计字段里记下的数字。

- **`ingest_runs` 的一行不会**在你后来单独跑 `ingest.py clean` 之后被改写成新数字；历史行永远保留当时那次 `run` 的摘要。
- 要看**现在**有多少条已清洗，请查 **`news_items`**，例如：

```sql
SELECT llm_clean_status, COUNT(*) FROM news_items GROUP BY llm_clean_status;
```

### 为什么界面里「好像一直未清洗」？

常见原因：

1. **看错行**：盯的是 **`ingest_runs.cleaned`**（历史快照），而不是 **`news_items.llm_clean_status`**。
2. **那一次 `run` 时清洗没真正跑成**：例如未加载 `.env`、缺 API Key、`BASE_URL` 曾经拼错（已通过自动补 `/v1` 修复）、或用了 `--no-clean`；此时 `ingest_runs.inserted` 可以很大，但 `cleaned` 仍为 0，且多数行会长期停在 `pending`（若 LLM 全部失败则会落到 `failed`）。
3. **积压条数多**：若设置了 `FINANCE_INGEST_LLM_CLEAN_MAX_ROUNDS_PER_RUN` 为正数，**单轮**只会洗掉部分批次，剩余仍是 `pending`，需要下一次 `run`/`clean` 继续。
4. **行数 2556 vs 256**：以 **`SELECT COUNT(*) FROM news_items`** 为准；和别的环境/备份库或看错表名时数字会对不上。

`ingest.py run` 成功时的 stdout 会带 **`cleaned`**、**`clean_rounds`**、**`pending_clean_remain`**，便于和库里对上。

---

## 下游依赖

- **finance-draft-manager**：读 DB（如 `db_snapshot.py`），组装开稿素材；**不**负责入库清洗
- **streamy-content-gen**：飞书「今日讯息」默认 **`query_market_facts.py`（DB）**；显式 `--live-fetch` 才走 `ingest.py legacy` 实时拉取。DB 路径可透传 **`--since-hours` / `--major-since-hours` / `--db-timeout` / `--no-router` / `--no-rewrite`**（见该脚本 `--help`）。

### Legacy 全量快照里的 `social_intelligence`（`scripts/pipeline.py` · `build_snapshot`）

与 Newsbox `ingest.py run` 并行存在的 **legacy 编排**仍会产出 `markdown_summary` 与完整 `sections`。其中社交情报增强约定如下：

- **汇总前去重**：`_dedupe_social_intel_items`（优先 `id` / `link` / `url` / `guid`，否则 `来源 + 发布时间 + 路由风格标题键`），避免同一条在多 bucket 重复抬高均值与 FG；`meta.social_intelligence.dedupe_*` 可观测输入/去重后条数。
- **逐条写回**：`enhance_social_intelligence` **原地**写入去重后的代表条目；`build_snapshot` 末尾再按 dedupe 键把同一逻辑条目的**其它 dict 副本**（如快讯列表与 Router 列表各持一份对象时）拷回相同量化字段，避免「一条有分、一条没有」。
- **聚合口径**：`avg_sentiment` 为简单平均；`platform_weighted_sentiment` 为按来源平台词典权重的加权平均；Markdown 与 `sentiment_label` 使用的 headline 为 **`headline_sentiment`**（有来源分组时与平台加权一致）。
- **恐惧贪婪**：`aggregate_metrics.fear_greed_scope` 为 `batch_relative` 时表示 FG 仅用**当次 run 内**逐条序列的分位；为 `db_run_history` 时表示已拼接 SQLite 表 **`social_intel_run_history`** 中最近 N 次（**legacy + ingest_run 合并**）的 headline / 池均 buzz，再在「run 级序列 + 本 run 聚合点」上算 FG；反转检测使用历史 FG（0–100）+ 当前 FG。表由 `storage` 幂等建表；**legacy** 成功后默认 append（`FINANCE_SOCIAL_INTEL_HISTORY_APPEND`）；**`ingest.py run`** 收尾默认 append（`FINANCE_SOCIAL_INTEL_INGEST_APPEND`）。**`db_snapshot.py`** 只读、不写。`storage.prune_old(days)` 与 `news_items` 等**同一 cutoff** 删除过期 `social_intel_run_history` 行。
- **DB 快照**：`finance-draft-manager/scripts/db_snapshot.py` 读库拼飞书摘要时，对窗口内新闻 / Router 结果 / 深度 / `sentiment_hot` 聚合后调用同一 **`enhance_social_intelligence`**（与 ingest 同源），并传入与 legacy 相同的历史序列（若库中已有 legacy 写入的 `social_intel_run_history`）。
