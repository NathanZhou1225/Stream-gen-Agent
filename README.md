# Stream-gen（workspace-stream-gen）

本目录可**单独**作为 Git 仓库发布（GitHub 根即此处）；也可作为 OpenClaw 整仓内的子目录 `workspace-stream-gen/`。

## 模型与 `.env` 怎么分工

- **主对话模型**：由 **OpenClaw / WorkBuddy 宿主**配置，本目录 `.env` **不强制** Ark/DeepSeek/Dashscope。
- **Router、板块润色（Rewriter）**：**默认开启**，须在 `.env` 中能解析 **OpenAI 兼容 `BASE_URL` + `API_KEY` + `MODEL`**（或宿主 `OPENCLAW_ARK_*` / `ARK_*` 回退）。关闭：`FINANCE_LLM_ROUTER_ENABLED=0` / `FINANCE_SECTOR_LLM_REWRITE_ENABLED=0`。  
- **ingest LLM 清洗**：在**云端 Worker**；业务机 **默认不校验**（勿设 `FINANCE_INGEST_LLM_CLEAN_ENABLED=1`，除非本机跑 `--live-fetch` legacy）。
- **P1（云端 API）**：校验 **`FINANCE_CLOUD_API_BASE_URL` + `FINANCE_CLOUD_API_KEY`** 并探测 `/health`；缺则 **`download_and_install.sh` exit 1**。临时跳过：`STREAM_GEN_SKIP_P1_READINESS=1`（不推荐生产）。联网 legacy 见 `--live-fetch`。
- **飞书**：选配；未配置时仅 `[OPTIONAL]` 提示，**不**阻断安装。

## 快速开始（魔法指令 · 简版）

将 `{REPO_URL}` 换成你的仓库地址；**WorkBuddy / 完整教练流程请用 [docs/DEPLOY_WORKBUDDY.md](docs/DEPLOY_WORKBUDDY.md) §4 整段 Prompt**（含 warm cron、开稿链冒烟、exec 超时）。

```text
请帮我部署 Workspace，目标仓库：{REPO_URL}

请严格按顺序执行，每步用一句话向我汇报结果：

1) git clone（若目录已存在则 git pull），进入仓库根（应有 download_and_install.sh、.env.example、scripts/）。
2) chmod +x download_and_install.sh && ./download_and_install.sh
3) 若含 [AGENT_INSTRUCTION_REQUIRED]：读 .env.example，向用户索要 ① FINANCE_CLOUD_API_BASE_URL + FINANCE_CLOUD_API_KEY；② FINANCE_LLM_ROUTER_* 三件套（Rewriter 可共用；或宿主 OPENCLAW_ARK_*）。勿索要 Tushare/RSSHub/ingest clean。飞书选配。勿编造密钥。
4) 写入本目录 .env 后再次 ./download_and_install.sh；须见 verify_env: OK 与 [DEPLOY_READINESS] p1=ok mode=cloud；再跑 openclaw_doctor。
5) 拉数验收（exec timeout ≥ 300 秒）：query_market_facts.py --sources market,news,social --summary-only
6) 可写 crontab 时：setup_snapshot_warm_cron.sh --install + warm_snapshot_cache.sh；推荐 monitor_snapshot_cache --probe-feishu-full 与 list-profile-options。
7) 有 [P1_GAP] 须置顶；仅当 bootstrap 成功且无 P1 缺口才可写「部署成功」。日常开稿见 DEPLOY_WORKBUDDY §3.3。
```

## 布局说明

| 布局 | `.env` |
|------|--------|
| **仅此仓库** | 只用本目录下的 `.env`（一份即可，变量见 `.env.example`） |
| **在 OpenClaw 整仓内** | 若上级目录存在 `openclaw.json`，会合并 **上级 `.env`** 与 **本目录 `.env`**（本目录覆盖同名变量） |

## 脚本

| 路径 | 作用 |
|------|------|
| `download_and_install.sh` | `pip` → `verify_env.py` → **`deploy_readiness.py`**；缺 `.env` 时 exit **10** |
| `scripts/verify_env.py` | **Router + Rewriter**（默认开）；ingest clean **仅** `FINANCE_INGEST_LLM_CLEAN_ENABLED=1` 时校 |
| `scripts/deploy_readiness.py` | **P1 云端 API + /health**；选配飞书 `[OPTIONAL]` |
| `scripts/upgrade.sh` | `git pull` + 再跑 bootstrap |
| `scripts/setup_snapshot_warm_cron.sh` | 业务机快照预热 cron（迁移/新路径必装） |
| `scripts/warm_snapshot_cache.sh` | 手动预热 → `cache/snapshot/snapshot.json` |
| `scripts/monitor_snapshot_cache.py` | 缓存新鲜度 + 飞书全量探针 |
| `scripts/openclaw_doctor.py` | 外网探测；失败打印 `OPENCLAW_DIAG` |

## 文档

- [DEPLOY.md](DEPLOY.md)  
- [docs/DEPLOY_WORKBUDDY.md](docs/DEPLOY_WORKBUDDY.md) — WorkBuddy 部署/更新 + 完整教练 Prompt  
- [SUPPORT_BUNDLE.md](SUPPORT_BUNDLE.md)

Python 建议 **3.10+**。
