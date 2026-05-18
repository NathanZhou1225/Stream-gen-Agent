# Stream-gen（workspace-stream-gen）

本目录可**单独**作为 Git 仓库发布（GitHub 根即此处）；也可作为 OpenClaw 整仓内的子目录 `workspace-stream-gen/`。

## 模型与 `.env` 怎么分工

- **主对话模型**：由 **OpenClaw / WorkBuddy 宿主**配置，本目录 `.env` **不强制** Ark/DeepSeek/Dashscope。
- **ingest LLM 清洗、Router、板块润色**：**默认开启**，须在 `.env` 中能解析出 **OpenAI 兼容 `BASE_URL` + `API_KEY` + `MODEL`**（或宿主 `OPENCLAW_ARK_*` / `ARK_*` 回退）。关闭请显式设 `FINANCE_*_ENABLED=0`（见 `.env.example`）。
- **P1（云端 API）**：校验 **`FINANCE_CLOUD_API_BASE_URL` + `FINANCE_CLOUD_API_KEY`** 并探测 `/health`；缺则 **`download_and_install.sh` exit 1**。临时跳过：`STREAM_GEN_SKIP_P1_READINESS=1`（不推荐生产）。联网 legacy 见 `--live-fetch`。
- **飞书**：选配；未配置时仅 `[OPTIONAL]` 提示，**不**阻断安装。

## 快速开始（魔法指令）

将 `{REPO_URL}` 换成你的仓库地址；**clone 后进入的目录即本仓库根**：

```text
请帮我部署 Workspace，目标仓库：{REPO_URL}

请严格按顺序执行，每步用一句话向我汇报结果：

1) git clone（若目录已存在则 git pull），进入仓库根目录（应能看到 download_and_install.sh、.env.example、scripts/）。
2) chmod +x download_and_install.sh && ./download_and_install.sh
3) 若输出含 [AGENT_INSTRUCTION_REQUIRED]，读取 .env.example，一次性向用户索要：① FINANCE_CLOUD_API_BASE_URL + FINANCE_CLOUD_API_KEY；② Router + Rewriter 的 FINANCE_* 三件套（或宿主 OPENCLAW_ARK_*）。勿索要 Tushare/RSSHub/ingest clean（仅 --live-fetch 需要）。飞书选配。不要编造密钥。
4) 用户以消息提供密钥后写入本目录 .env；总结中不要复述完整密钥。
5) 再次运行 ./download_and_install.sh；必须看到 [DEPLOY_READINESS] p1=ok 且无 verify_env 错误。再运行：python3 scripts/openclaw_doctor.py --repo-root "$(pwd)"
6) 若存在 [P1_GAP] 行，必须在结论中置顶说明并要求补配；仅当两步脚本均成功且无 P1 缺口时，才允许写「部署成功」。
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
| `scripts/verify_env.py` | ingest clean / Router / Rewriter（**默认开**）三件套 + 宿主回退 |
| `scripts/deploy_readiness.py` | **P1 云端 API + /health**；选配飞书 `[OPTIONAL]` |
| `scripts/upgrade.sh` | `git pull` + 再跑 bootstrap |
| `scripts/openclaw_doctor.py` | 外网探测；失败打印 `OPENCLAW_DIAG` |

## 文档

- [DEPLOY.md](DEPLOY.md)  
- [docs/DEPLOY_WORKBUDDY.md](docs/DEPLOY_WORKBUDDY.md) — WorkBuddy 部署/更新 + 完整教练 Prompt  
- [SUPPORT_BUNDLE.md](SUPPORT_BUNDLE.md)

Python 建议 **3.10+**。
