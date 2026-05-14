# Stream-gen（workspace-stream-gen）

本目录可**单独**作为 Git 仓库发布（GitHub 根即此处）；也可作为 OpenClaw 整仓内的子目录 `workspace-stream-gen/`。

## 快速开始（魔法指令）

将 `{REPO_URL}` 换成你的仓库地址；**clone 后进入的目录即本仓库根**：

```text
请帮我部署 Workspace，目标仓库：{REPO_URL}

请严格按顺序执行，每步用一句话向我汇报结果：

1) git clone（若目录已存在则 git pull），进入仓库根目录（应能看到 download_and_install.sh、.env.example、scripts/）。
2) chmod +x download_and_install.sh && ./download_and_install.sh
3) 若输出含 [AGENT_INSTRUCTION_REQUIRED]，读取本目录 .env.example，列出必填（至少一个 LLM Key）与选填变量，停止自动化，向我一次性索要缺失项。
4) 在我提供密钥后，在本目录创建或覆盖 .env；不要在总结中复述完整密钥。
5) 再次运行 ./download_and_install.sh；成功后运行：python3 scripts/openclaw_doctor.py --repo-root "$(pwd)"
6) 给出部署结论；若有 OPENCLAW_DIAG 行，解释并指向 DEPLOY.md。
```

## 布局说明

| 布局 | `.env` |
|------|--------|
| **仅此仓库** | 只用本目录下的 `.env`（一份即可，变量见 `.env.example`） |
| **在 OpenClaw 整仓内** | 若上级目录存在 `openclaw.json`，会合并 **上级 `.env`** 与 **本目录 `.env`**（本目录覆盖同名变量） |

## 脚本

| 路径 | 作用 |
|------|------|
| `download_and_install.sh` | `pip install -r requirements.txt`；缺可用 `.env` 时 `[AGENT_INSTRUCTION_REQUIRED]` + **exit 10**；否则 `scripts/verify_env.py` |
| `scripts/verify_env.py` | 校验 LLM 必填组 |
| `scripts/openclaw_doctor.py` | 外网探测；失败打印 `OPENCLAW_DIAG` |

## 文档

- [DEPLOY.md](DEPLOY.md)  
- [SUPPORT_BUNDLE.md](SUPPORT_BUNDLE.md)

Python 建议 **3.10+**。
