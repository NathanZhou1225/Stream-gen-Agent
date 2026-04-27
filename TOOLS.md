# TOOLS.md

技能（`SKILL.md`）定义「如何做事」；本文件只记**本机 / 本 agent** 的约定与开关。

---

## 飞书连接与路由（独立 chatbot = 独立 accountId）

与「一个微信 im-bot 账号 → 一个 binding」同思路：

| 项 | 值 |
|----|-----|
| `channels.feishu.accounts` | 本机器人：`stream-gen`（`appId` / `appSecret` 填飞书**单独**企业应用的凭证） |
| `bindings` | `channel: feishu` + `accountId: stream-gen` → agent **`stream-gen`** |

1. 飞书开放平台新建**仅给本 agent 用**的机器人应用，把 **App ID、App Secret** 写入 `openclaw.json` 里 `channels.feishu.accounts.stream-gen`。
2. 在飞书后台配置事件/订阅/加密字段时，把插件文档要求的 `encryptKey`、`verificationToken` 等一并落在**同一** `stream-gen` 段内（与你在另一个账号/机器人里的写法保持一致）。
3. 确认 **`feishu-openclaw-plugin`** 已启用；**不要**让两套飞书 channel 插件同时绑同一飞书应用。
4. 改配置后**重启** OpenCl 网关，再发消息到该 chatbot 验证进线。

---

## 凭据（_credentials skill）

若某 skill 需要**按 agent 隔离**的 key，用仓库内 `~/.openclaw/skills/_credentials` 的脚本；**不要**把完整 key 写进 `SOUL` / `USER` / `MEMORY.md`。

- 读：`\~/.openclaw/skills/_credentials/scripts/read-cred <type> ...`
- 写：`\~/.openclaw/skills/_credentials/scripts/write-cred <type> KEY=VALUE ...`

**已登记 type：** 与具体 skill 联用时再填表（当前无强绑定）。

---

## Streamy 工作区技能（仅本 workspace 发现）

OpenCl 从 **`/root/.openclaw/workspace-stream-gen/skills/*/`** 子目录读 `SKILL.md`（勿放在 workspace 根，否则不进列表）：

- `skills/streamy-content-gen/` — 多阶段成稿、preflight 等  
- `skills/finance-source-ingest/` — 事实/信源 `ingest.py`；`preflight_topic.py` 默认同级兄弟路径找该目录  

**迁机 / 换 workspace 路径**：勿拷贝旧 `.venv`。首次在目标机用 **`python3 scripts/ingest.py ...`** 即会**自动**建 venv 并 `pip install`（见 `scripts/_venv_bootstrap.py`），或手跑 **`./scripts/bootstrap_venv.sh`**。离线/禁自动：`FINANCE_INGEST_NO_AUTO_VENV=1`。

`docs/` 为说明/引用，不单独参与技能发现。

## 文件系统

- 可写范围默认以 **本 workspace** 与工具策略为准（`openclaw.json` 里本 agent 的 `fs.workspaceOnly`）。
