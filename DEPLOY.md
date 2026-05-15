# 部署与运维（workspace-stream-gen）

本文件以 **本目录为仓库根** 为准；若嵌在 OpenClaw 整仓，见 README「布局说明」。

## pip / PEP 668

若 `pip install` 因 **externally-managed-environment** 失败，bootstrap 会自动重试 **`--break-system-packages`**。若你希望只用虚拟环境，可先 `python3 -m venv .venv && . .venv/bin/activate`，并设置 **`STREAM_GEN_PIP_NO_BREAK=1`** 再运行 `./download_and_install.sh`（此时第一遍 pip 失败且不会自动加 break，你应在 venv 内自行装好依赖）。

## 退出码：`download_and_install.sh`

| 退出码 | 含义 |
|--------|------|
| **0** | 依赖已安装，`verify_env` 与 **`deploy_readiness`** 均通过 |
| **10** | 缺可用 `.env`（独立仓：本目录无 `.env`；整仓子目录：本目录与上级均无 `.env`）；含 `[AGENT_INSTRUCTION_REQUIRED]` |
| **1** | pip、`verify_env` 失败，或 **P1 缺口**（`[P1_GAP]`，除非 `STREAM_GEN_SKIP_P1_READINESS=1`） |

## `deploy_readiness.py`（P1 + 选配）

在 `verify_env` 通过后由 **`download_and_install.sh`** 自动调用；也可手动：

```bash
python3 scripts/deploy_readiness.py --repo-root "$(pwd)"
```

- **P1 默认（`FINANCE_CLOUD_MODE=1`）**：`FINANCE_CLOUD_API_BASE_URL`、`FINANCE_CLOUD_API_KEY` 有效，且能访问 `{BASE_URL}/health`。  
- **P1 Advanced（`FINANCE_CLOUD_MODE=0`）**：本机 `TUSHARE_TOKEN`、`FINANCE_RSSHUB_BASE_URL`（本地 ingest + SQLite）。  
- **跳过（不推荐生产）**：`STREAM_GEN_SKIP_P1_READINESS=1`。  
- 成功：`[DEPLOY_READINESS] p1=ok mode=cloud optional=see_above`

## `.env` 合并与进程环境（`scripts/verify_env.py` / `openclaw_doctor.py` / `deploy_readiness.py`）

1. 先从磁盘合并 `.env`（规则见下）。  
2. 再对「文件中未出现或为空」的键，用**当前进程环境**补齐（便于宿主 OpenClaw 注入 `OPENCLAW_*` 等，无需写进仓库）。

**磁盘合并顺序**

1. 若 `--repo-root` 下存在子目录 **`workspace-stream-gen/`**（OpenClaw 根）：先读 `repo_root/.env`，再读 `repo_root/workspace-stream-gen/.env`（后者覆盖）。  
2. 若 `repo_root` 的**上级**存在 **`openclaw.json`**：先读 `parent/.env`，再读 `repo_root/.env`（后者覆盖）。  
3. 否则（**独立 GitHub 仓**）：只读 `repo_root/.env`。

## `verify_env` 校验什么（不再要求「三选一 Ark/DeepSeek/Dashscope」）

- **ingest LLM 清洗**（默认开）：须能解析 **BASE_URL + API_KEY + MODEL**；关闭：`FINANCE_INGEST_LLM_CLEAN_ENABLED=0`。  
- **Router**（**默认开**）：同上；关闭：`FINANCE_LLM_ROUTER_ENABLED=0`。  
- **Rewriter**（**默认开**）：同上；关闭：`FINANCE_SECTOR_LLM_REWRITE_ENABLED=0`。

## doctor

在**本仓库根**执行：

```bash
python3 scripts/openclaw_doctor.py --repo-root "$(pwd)"
```

- `--skip-probes`：只做变量检查。  
- 失败时会出现 `OPENCLAW_DIAG {...}`（不含密钥）。  
- 仍会探测磁盘/进程里已配置的 **Ark / DeepSeek / Dashscope / Router**（若对应变量存在）。

### 锚点

- **doctor**：见上。  
- **finance-llm**：`FINANCE_LLM_ROUTER_*`  
- **rsshub**：`FINANCE_RSSHUB_BASE_URL` 可达性；**P1 必填**另见 `deploy_readiness.py`

## 问财 SkillHub zip

仅当你本地还有完整 OpenClaw 仓时，脚本可能在 `../scripts/iwencai_skillhub_download_and_install.sh`；**独立本仓发布时可忽略**。

## 云端 Newsbox（v0.3.0 · `finance-ingest-cloud`）

**只 clone 本仓（`workspace-stream-gen`）时**：不在客户端配置 `TUSHARE_TOKEN` / `FINANCE_RSSHUB_BASE_URL` / ingest clean 密钥；由运维在 **`/path/to/finance-ingest-cloud`** 部署 MySQL + Worker + FastAPI。

客户端 `.env`（拉数）：

```bash
FINANCE_CLOUD_MODE=1
FINANCE_CLOUD_API_BASE_URL=https://your-cloud-host:8080
FINANCE_CLOUD_API_KEY=your-bearer-secret
```

验收：

```bash
python3 skills/streamy-content-gen/scripts/query_market_facts.py --cloud --sources market,news,social --summary-only
```

- 云端返回 **pre-Router** `sections`；本地 **`db_snapshot`** 仍跑 Router/Rewriter（须配置 `FINANCE_LLM_ROUTER_*` / `FINANCE_SECTOR_LLM_*` 或宿主回退）。
- 未开云模式时行为不变：默认读本地 `user_data/finance_sources.db`。
- API 运维文档：`../finance-ingest-cloud/README.md`（Monorepo 同级目录）。

### 其它服务器上的 Agent（只 clone 本仓）

**数据面**在运维机部署 `finance-ingest-cloud`（MySQL + Worker + `./run_api.sh`，`FINANCE_CLOUD_API_HOST=0.0.0.0`）。**业务机**只需 Git clone **`workspace-stream-gen`**，不必部署 RSSHub/Tushare/ingest Worker。

业务机 `workspace-stream-gen/.env` 最少：

```bash
FINANCE_CLOUD_MODE=1
FINANCE_CLOUD_API_BASE_URL=http://<云端服务器IP或域名>:8080
FINANCE_CLOUD_API_KEY=<运维下发的 Bearer 密钥，与云端 FINANCE_CLOUD_API_KEYS 中 secret 一致>
# 本地仍须 Router/Rewriter 三件套（清洗已在云端完成）
FINANCE_LLM_ROUTER_BASE_URL=...
FINANCE_LLM_ROUTER_API_KEY=...
```

拉数（与飞书 Agent 相同入口）：

```bash
python3 skills/streamy-content-gen/scripts/query_market_facts.py --sources market,news,social --summary-only
```

链路：HTTP 拉 **pre-Router** JSON → 本机 `db_snapshot` 套 Router/Rewriter → 输出 `markdown_summary`。

**安全组**：在云端机器放行 TCP **8080**（或反代 443）。生产建议 HTTPS 反代，勿把 Key 提交 Git。

**API Key 不是「公开给全世界」**：谁持有 `FINANCE_CLOUD_API_KEY` 谁就能拉数；按租户可配多把 Key（`FINANCE_CLOUD_API_KEYS=teamA:xxx,teamB:yyy`）。

### 公网 `curl http://<公网IP>:8080/health` 超时怎么排查？

**现象**：在云端机器本机上 `curl http://101.96.194.178:8080` 超时，但 `curl http://127.0.0.1:8080/health` 返回 200。

**原因（常见两类，可同时存在）**

1. **在云服务器里不要用公网 IP 测自己**（NAT 回环/hairpin 多数云不支持）。本机 / 同机 Agent 请用：
   - `http://127.0.0.1:8080`（推荐）
   - 或内网 `http://172.31.0.2:8080`
2. **安全组未放行 8080**：其它服务器、你笔记本从外网访问时，须在云控制台给该 ECS **入站 TCP 8080**（来源填业务机 IP 或按需 `0.0.0.0/0`）。

**本机快速自检（在 `iv-yek...` 上执行）**

```bash
curl -sS http://127.0.0.1:8080/health          # 应 200
curl -sS http://172.31.0.2:8080/health         # 应 200
ss -tlnp | grep 8080                           # 应 0.0.0.0:8080
```

**外网是否真通**：请在「另一台机器」或笔记本上测（不要在被测 ECS 上 curl 自己的公网 IP）：

```bash
curl -sS --connect-timeout 5 http://101.96.194.178:8080/health
```

- 仍超时 → 去云控制台改**安全组**（火山引擎 / 阿里云等：**云服务器 → 实例 → 安全组 → 入方向 → 添加规则**）：
  - 协议：TCP
  - 端口：8080
  - 源：业务机公网 IP/32，或内网网段
- 外网 200、仅本机 curl 公网 IP 失败 → 正常，本机用 `127.0.0.1` 即可。

**同 VPC 其它服务器**：`FINANCE_CLOUD_API_BASE_URL=http://172.31.0.2:8080`（不必走公网）。

**跨公网其它服务器**：`FINANCE_CLOUD_API_BASE_URL=http://101.96.194.178:8080` + 安全组放行 8080。

### 完全外网 Agent（跨机房 / 家里笔记本 / 其它云）

服务端（本 ECS）已监听 `0.0.0.0:8080`。**你必须在云平台控制台放行入站 TCP 8080**，否则外网永远超时。

**火山引擎（主机名 `iv-yek*` 多为该类）**

1. 登录 [火山引擎控制台](https://console.volcengine.com/) → **云服务器 ECS** → 找到实例 `iv-yek7o43r40h2cbensb54`（或对应实例 ID）。
2. 进入实例 → **安全组** → **配置规则** → **入方向** → **添加规则**：
   - 协议：**TCP**
   - 端口：**8080**
   - 源地址：业务机公网 IP + `/32`（更安全）；临时调试可用 `0.0.0.0/0`
   - 策略：允许
3. 保存后等约 1 分钟，在**外网电脑**（不要在这台 ECS 上）执行：

```bash
curl -sS --connect-timeout 5 http://101.96.194.178:8080/health
```

应返回 `{"ok":true,...}`。

**外网业务机 `workspace-stream-gen/.env`（只 clone 本仓）**

```bash
FINANCE_CLOUD_MODE=1
FINANCE_CLOUD_API_BASE_URL=http://101.96.194.178:8080
FINANCE_CLOUD_API_KEY=<运维私下发放的 secret，与云端 FINANCE_CLOUD_API_KEYS 一致>
FINANCE_CLOUD_API_TIMEOUT=60
# 本地 Router/Rewriter 仍须自备 LLM 三件套
FINANCE_LLM_ROUTER_BASE_URL=...
FINANCE_LLM_ROUTER_API_KEY=...
```

**注意**：在**数据机本机**请继续用 `http://127.0.0.1:8080`；`curl http://101.96.194.178:8080` 在本机上失败是正常现象。

若公司政策**不能开 8080**：可改为 Nginx/Caddy 反代到 `127.0.0.1:8080`，安全组只开 **443**，`BASE_URL` 改为 `https://你的域名`（需另配证书）。

## 安全

勿提交 `.env`；对外工单用 `SUPPORT_BUNDLE.md`，勿贴全密钥。
