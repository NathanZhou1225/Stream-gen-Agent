"""稿件类型模板：加载 JSON、渲染 {{var}}、导出 prompt / JSON Schema、Assembler、segments 草稿。

设计对齐总账「主链脚本不调 LLM」：本模块只准备结构化约束与文本拼装；模型调用在 Agent/网关侧完成。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from models.schemas import build_modules_json_schema, template_ordered_keys

_VAR = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"JSON 根须为 object: {path}")
    return data


def render_mustache(text: str, variables: dict[str, Any]) -> str:
    """仅支持 `{{ var }}` 单层替换；缺变量抛 KeyError。"""

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in variables:
            raise KeyError(f"模板占位符未在 IP 配置中找到: {name}")
        return str(variables[name])

    return _VAR.sub(repl, text)


def collect_placeholders(text: str) -> set[str]:
    return set(_VAR.findall(text))


def load_template(content_type: str) -> dict[str, Any]:
    root = skill_root()
    path = root / "configs" / "content_templates" / f"{content_type}.json"
    if not path.is_file():
        raise FileNotFoundError(f"未找到模板: {path}")
    return load_json(path)


def load_ip_profile(ip_id: str) -> dict[str, Any]:
    root = skill_root()
    path = root / "configs" / "ip_profiles" / f"{ip_id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"未找到 IP 配置: {path}")
    return load_json(path)


def render_template_instructions(template: dict[str, Any], variables: dict[str, Any]) -> list[dict[str, str]]:
    schema = template.get("schema")
    if not isinstance(schema, list):
        raise ValueError("模板缺少 schema 数组")
    out: list[dict[str, str]] = []
    for row in schema:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "").strip()
        instr = str(row.get("instruction") or "")
        if not key:
            continue
        rendered = render_mustache(instr, variables)
        out.append({"key": key, "instruction": rendered})
    return out


def build_prompt_bundle(
    template: dict[str, Any],
    rendered_rows: list[dict[str, str]],
    *,
    style_text: str,
    sources_text: str,
    topic_text: str,
) -> dict[str, Any]:
    ctype = str(template.get("type") or "")
    desc = str(template.get("description") or "")
    system = (
        "你是金融短视频口播稿专家。严格遵守用户给定的「风格」与「事实材料」，"
        "不得编造具体涨跌幅、日期、板块表现等可验证事实，除非材料中已给出。\n"
        "输出必须是 JSON object，且只能包含指定键，每键一段口播正文（字符串）。"
    )
    blocks = [f"【稿件类型】{ctype}\n{desc}\n"]
    if style_text.strip():
        blocks.append(f"【风格】\n{style_text.strip()}\n")
    if topic_text.strip():
        blocks.append(f"【选题/标题方向】\n{topic_text.strip()}\n")
    if sources_text.strip():
        blocks.append(f"【信源/事实材料】\n{sources_text.strip()}\n")
    blocks.append("【分段生成要求】（按 key 产出对应段落；顺序无关但键必须齐全）\n")
    for r in rendered_rows:
        blocks.append(f"- `{r['key']}`: {r['instruction']}\n")
    user = "".join(blocks)
    keys = [r["key"] for r in rendered_rows]
    desc_map = {r["key"]: r["instruction"][:200] for r in rendered_rows}
    return {
        "system": system,
        "user": user,
        "content_type": ctype,
        "required_module_keys": keys,
        "json_schema": build_modules_json_schema(keys, descriptions=desc_map),
    }


def assemble_plain_text(modules: dict[str, str], key_order: list[str]) -> str:
    parts: list[str] = []
    for k in key_order:
        if k not in modules:
            raise KeyError(f"模块 JSON 缺少键: {k}")
        text = str(modules[k] or "").strip()
        if not text:
            raise ValueError(f"模块 `{k}` 为空字符串，拒绝拼接")
        parts.append(text)
    return "\n\n".join(parts)


def modules_to_segments(
    modules: dict[str, str],
    key_order: list[str],
    draft_roles: list[str],
    *,
    duration_sec: int,
) -> list[dict[str, Any]]:
    if len(draft_roles) != len(key_order):
        raise ValueError("draft_segment_roles 与 schema 键数量不一致")
    n = len(key_order)
    segments: list[dict[str, Any]] = []
    for i, (key, role) in enumerate(zip(key_order, draft_roles)):
        start = int(duration_sec * i / n)
        end = int(duration_sec * (i + 1) / n) - 1
        if end < start:
            end = start
        t0 = f"{start // 60}:{start % 60:02d}"
        t1 = f"{end // 60}:{end % 60:02d}"
        say = str(modules.get(key) or "").strip()
        if not say:
            raise ValueError(f"模块 `{key}` 为空")
        seg: dict[str, Any] = {
            "time": f"{t0}-{t1}",
            "role": role,
            "say": say,
        }
        if role in {"argument_1", "argument_2", "argument_3", "argument", "turn"}:
            seg["claim_kind"] = "mixed"
            seg["evidence_source_type"] = "user_judgement"
            seg["evidence_source_ref"] = f"模块:{key}"
        segments.append(seg)
    return segments


def cmd_schema(args: argparse.Namespace) -> int:
    template = load_template(args.content_type)
    schema_list = template.get("schema")
    if not isinstance(schema_list, list):
        raise SystemExit("模板 schema 非法")
    keys = template_ordered_keys(schema_list)
    desc = {
        str(r.get("key")): str(r.get("instruction") or "")[:500]
        for r in schema_list
        if isinstance(r, dict) and r.get("key")
    }
    js = build_modules_json_schema(keys, descriptions=desc)
    print(json.dumps(js, ensure_ascii=False, indent=2))
    return 0


def cmd_prompt_bundle(args: argparse.Namespace) -> int:
    template = load_template(args.content_type)
    variables: dict[str, Any] = {}
    if args.ip_id:
        profile = load_ip_profile(args.ip_id)
        variables = {k: v for k, v in profile.items() if not k.startswith("_")}
    # 校验：模板中若出现占位符则必须能解析
    schema_list = template.get("schema")
    if isinstance(schema_list, list):
        for row in schema_list:
            if isinstance(row, dict):
                instr = str(row.get("instruction") or "")
                for ph in collect_placeholders(instr):
                    if ph not in variables:
                        raise SystemExit(
                            f"instruction 含 `{{{{ {ph} }}}}` 但未提供 `--ip-id` 或 IP 文件缺该字段"
                        )
    rendered = render_template_instructions(template, variables)
    style = Path(args.style_file).read_text(encoding="utf-8") if args.style_file else ""
    sources = Path(args.sources_file).read_text(encoding="utf-8") if args.sources_file else ""
    topic = Path(args.topic_file).read_text(encoding="utf-8") if args.topic_file else ""
    bundle = build_prompt_bundle(template, rendered, style_text=style, sources_text=sources, topic_text=topic)
    if args.compact_json:
        print(json.dumps(bundle, ensure_ascii=False))
    else:
        print(json.dumps(bundle, ensure_ascii=False, indent=2))
    return 0


def cmd_assemble(args: argparse.Namespace) -> int:
    template = load_template(args.content_type)
    keys = template_ordered_keys(template.get("schema") or [])
    raw = sys_stdin_read()
    modules = json.loads(raw)
    if not isinstance(modules, dict):
        raise SystemExit("stdin 须为 JSON object")
    text = assemble_plain_text({k: str(v) for k, v in modules.items()}, keys)
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def cmd_segments(args: argparse.Namespace) -> int:
    template = load_template(args.content_type)
    keys = template_ordered_keys(template.get("schema") or [])
    roles = template.get("draft_segment_roles")
    if not isinstance(roles, list) or len(roles) != len(keys):
        raise SystemExit("模板缺少 draft_segment_roles 或与键数量不一致")
    roles_s = [str(x) for x in roles]
    raw = sys_stdin_read()
    modules = json.loads(raw)
    if not isinstance(modules, dict):
        raise SystemExit("stdin 须为 JSON object")
    segs = modules_to_segments(
        {k: str(v) for k, v in modules.items()},
        keys,
        roles_s,
        duration_sec=int(args.duration_sec),
    )
    out = {"segments": segs, "content_type": template.get("type"), "module_keys": keys}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def sys_stdin_read() -> str:
    return sys.stdin.read()


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="稿件类型模板工具（无 LLM 调用）")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--content-type",
        required=True,
        choices=["market_view", "investor_edu", "persona_intro"],
        help="与 configs/content_templates/<name>.json 对应",
    )

    s1 = sub.add_parser("schema", parents=[common], help="打印模块 JSON Schema（draft-07）")
    s1.set_defaults(func=cmd_schema)

    s2 = sub.add_parser("prompt-bundle", parents=[common], help="输出 system/user + json_schema")
    s2.add_argument("--ip-id", default="", help="加载 configs/ip_profiles/<id>.json 渲染占位符")
    s2.add_argument("--style-file", default="", help="风格文本文件路径")
    s2.add_argument("--sources-file", default="", help="信源/摘要文本")
    s2.add_argument("--topic-file", default="", help="选题/标题方向文本")
    s2.add_argument("--compact-json", action="store_true")
    s2.set_defaults(func=cmd_prompt_bundle)

    s3 = sub.add_parser("assemble", parents=[common], help="从 stdin 读模块 JSON，按模板顺序拼接口播")
    s3.set_defaults(func=cmd_assemble)

    s4 = sub.add_parser("segments", parents=[common], help="从 stdin 读模块 JSON，生成 segments[] 草案")
    s4.add_argument("--duration-sec", type=int, default=60)
    s4.set_defaults(func=cmd_segments)

    return p


def main(argv: list[str] | None = None) -> int:
    ap = build_arg_parser()
    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
