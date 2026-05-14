# 部署与运维（workspace-stream-gen）

本文件以 **本目录为仓库根** 为准；若嵌在 OpenClaw 整仓，见 README「布局说明」。

## pip / PEP 668

若 `pip install` 因 **externally-managed-environment** 失败，bootstrap 会自动重试 **`--break-system-packages`**。若你希望只用虚拟环境，可先 `python3 -m venv .venv && . .venv/bin/activate`，并设置 **`STREAM_GEN_PIP_NO_BREAK=1`** 再运行 `./download_and_install.sh`（此时第一遍 pip 失败且不会自动加 break，你应在 venv 内自行装好依赖）。

## 退出码：`download_and_install.sh`

| 退出码 | 含义 |
|--------|------|
| **0** | 依赖已安装且 `verify_env` 通过 |
| **10** | 缺可用 `.env`（独立仓：本目录无 `.env`；整仓子目录：本目录与上级均无 `.env`）；含 `[AGENT_INSTRUCTION_REQUIRED]` |
| **1** | pip 或 `verify_env` 失败 |

## `.env` 合并（`scripts/verify_env.py` / `openclaw_doctor.py`）

1. 若 `--repo-root` 下存在子目录 **`workspace-stream-gen/`**（OpenClaw 根）：先读 `repo_root/.env`，再读 `repo_root/workspace-stream-gen/.env`（后者覆盖）。  
2. 若 `repo_root` 的**上级**存在 **`openclaw.json`**：先读 `parent/.env`，再读 `repo_root/.env`（后者覆盖）。  
3. 否则（**独立 GitHub 仓**）：只读 `repo_root/.env`。

**必填组**：`ARK_API_KEY`、`DEEPSEEK_API_KEY`、`DASHSCOPE_CODING_API_KEY` 至少其一非占位（见 `.env.example`）。

## doctor

在**本仓库根**执行：

```bash
python3 scripts/openclaw_doctor.py --repo-root "$(pwd)"
```

- `--skip-probes`：只做变量检查。  
- 失败时会出现 `OPENCLAW_DIAG {...}`（不含密钥）。

### 锚点

- **doctor**：见上。  
- **finance-llm**：`FINANCE_LLM_ROUTER_*`  
- **rsshub**：`DEPLOY.md` 本节即 `FINANCE_RSSHUB_BASE_URL` 可达性

## 问财 SkillHub zip

仅当你本地还有完整 OpenClaw 仓时，脚本可能在 `../scripts/iwencai_skillhub_download_and_install.sh`；**独立本仓发布时可忽略**。

## 安全

勿提交 `.env`；对外工单用 `SUPPORT_BUNDLE.md`，勿贴全密钥。
