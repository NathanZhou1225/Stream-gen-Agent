# streamy-content-gen

短视频/口播内容产出 **skill**：`topic_picking` → `outline_refining` → `script_refining`、Draft 生命周期、合规与事实管道。权威入口为仓库内 [`SKILL.md`](./SKILL.md)。

## 本目录要点

| 项 | 说明 |
|----|------|
| Playbook / 铁律 | [`SKILL.md`](./SKILL.md) |
| 带方向开稿前编排 | `scripts/preflight_topic.py`（stdout 单 JSON，subprocess 调 `finance-source-ingest`） |
| Draft 生命周期 | `scripts/draft_manager.py`（若缺少 `_common.py` / `script_renderer.py` 等，见下「源文件与 .pyc」） |
| 提示词 | `prompts/`，`references/`，`templates/` |
| 阶段状态与路线图 | 见 workspace [`docs/PRODUCT-STATUS-*.md`](../../docs/) |

## 依赖

- 核心脚本为 Python 3.12+；`draft_manager` 等依赖同目录可导入模块。
- 可选：根目录若存在 `requirements.txt`（历史版本），`tushare` 等为**可选**信源；与 `preflight` 主路径可分离。

## 源文件与 `.pyc`（重要）

部分 `scripts/*.py` 若仅有 `__pycache__/*.cpython-312.pyc` 而缺 `.py` 源，Python **无法**直接 `import` 该模块。恢复方式二选一：

1. **自备份/其它机器**拷回 `*_common.py`、`script_renderer.py`、`lite_compliance_scan.py` 等。  
2. 使用 **decompyle++ (pycdc)** 等对 **Python 3.12 字节码** 反编并**手工**修正（3.12 在部分工具上无官方反编译；项目内曾用 pycdc + `dis` 校验）。

当前仓库中 **存在** 的源文件以 `ls scripts/*.py` 为准。

## 相关

- 产品 PRD/阶段：[`/workspace-streamy/docs/`](../../docs/)
- 事实管道相邻技能：[`/workspace-streamy/skills/finance-source-ingest`](../finance-source-ingest)
- 个性化风格记忆：[`user-style-manager`](../user-style-manager)（`draft` 上 `style_id` + 成稿前 `user_style_context`）

## Changelog（摘录）

- **0.1.7.3**：`draft_manager` 支持 `meta.style_id`（`create --style-id`，`update --set-style-id` / `--clear-style`）；与 `user-style-manager` 的 `user_style_context` 约定见 [`SKILL.md`](./SKILL.md)。
