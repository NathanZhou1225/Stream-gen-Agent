# finance-source-ingest（联网 legacy 采集）v0.3.1

## 角色定位

**金融信息实时抓取（Legacy Layer）**。飞书/clone 用户侧**不跑**本地 `ingest.py run`；**定时入库**仅在运维机 `finance-ingest-cloud`（MySQL，见该目录 `README.md` cron 表）。

- **本 skill 保留**：`ingest.py legacy`（`pipeline.build_snapshot`）与 `repair-rsshub`
- **日常拉数**：`streamy-content-gen/scripts/query_market_facts.py` → 云端 API（默认）或 `--live-fetch`（本 skill legacy）

### 主路径 vs Legacy

| 入口 | 说明 |
|------|------|
| `query_market_facts.py`（默认） | HTTP 云端 pre-Router → 本地 `db_snapshot --pre-router-stdin` |
| `ingest.py legacy` | 实时全量 `markdown_summary`（慢，需网络；显式 `--live-fetch` / `preflight --source-mode legacy`） |

---

## 调用约定

### legacy 实时快照

```bash
python scripts/ingest.py legacy \
  --sources market,news,social \
  --keywords "AI 算力" \
  --max-items 30 \
  --out-dir /path/to/out
```

或由统一入口：

```bash
python3 skills/streamy-content-gen/scripts/query_market_facts.py \
  --live-fetch --sources market,news,social --summary-only
```

### repair-rsshub

```bash
python scripts/ingest.py repair-rsshub --decision confirm
```

---

## 目录结构（节选）

```
finance-source-ingest/
  scripts/
    ingest.py         # cloud Worker：`run`（Newsbox）；本机常用 `legacy` + `repair-rsshub`
    pipeline.py       # legacy build_snapshot（勿扩展 Router）
  collectors/         # 仍被 cloud Worker 与 legacy 共用
```

运维入库：`../finance-ingest-cloud/worker/run_ingest.sh` + `setup_cron.sh`（MySQL）。北京时间 **08:00** `--sources news` · **09:40 / 14:00 / 20:00** `--sources market,news,social`。
