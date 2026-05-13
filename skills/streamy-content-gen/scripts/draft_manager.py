# Reconstructed from Python 3.12 .pyc (pycdc + `dis`); verified against bytecode.
'''Draft 生命周期管理脚本（streamy-content-gen）。

子命令：
    create     创建新 Draft，返回 draft_id + 初始 stage
    list       列出当前 user 的所有 active Draft
    show       展示指定 Draft 的最新状态（meta + 当前阶段产物）
    switch     切换焦点 Draft
    update     更新 Draft 某阶段产物（topic/outline/script）+ append history
    finalize   定稿归档（active → archive）
    drop       放弃归档（active → archive，标记 dropped）

所有命令统一 --json 输出，错误时 ok=false + error_type/error_code/message。

使用约定：
    python3 draft_manager.py <subcommand> [options] --json
'''
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from _common import (
    CST,
    STAGE_DROPPED,
    STAGE_FINALIZED,
    STAGE_OUTLINE,
    STAGE_SCRIPT,
    STAGE_TOPIC,
    assert_stage_transition,
    emit_error,
    emit_ok,
    ensure_user_entry,
    gen_draft_id,
    get_active_draft_dir,
    get_archive_root,
    get_user_id,
    now_iso,
    read_index,
    read_json,
    today_date_str,
    write_index,
    write_json_atomic,
    write_text_atomic,
)
from script_renderer import RENDERER_VERSION, ScriptRenderError, render_script_md

STAGE_ARTIFACTS = {
    STAGE_SCRIPT: {
        'json': 'script.json',
        'md': 'script.md' },
    STAGE_OUTLINE: {
        'json': 'outline.json',
        'md': 'outline.md' },
    STAGE_TOPIC: {
        'json': 'topic_candidates.json',
        'md': None } }
STAGE_ORDER = [
    STAGE_TOPIC,
    STAGE_OUTLINE,
    STAGE_SCRIPT]
VALID_EVIDENCE_CONFIDENCE = {'high', 'medium', 'low'}
MAX_OUTLINE_PRODUCTION_HINT_LEN = 36
SCRIPT_APPENDIX_SECTIONS = (
    'camera_shots',
    'stickers_effects',
    'visual_assets',
    'host_actions',
)


def _truthy_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return raw in ("1", "true", "TRUE", "yes", "YES", "on", "ON")


def _run_min_context_reset() -> dict[str, Any]:
    workspace_root = Path(__file__).resolve().parents[3]
    reset_py = workspace_root / "scripts" / "reset_min_context.py"
    if not reset_py.is_file():
        return {"ok": False, "error": f"reset script missing: {reset_py}"}
    try:
        cp = subprocess.run(
            [sys.executable, str(reset_py), "--keep-latest", "1", "--compact-sessions-json"],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            timeout=40,
            check=False,
        )
        if cp.returncode != 0:
            return {"ok": False, "error": (cp.stderr or cp.stdout or "").strip()[:500]}
        out = (cp.stdout or "").strip()
        if not out:
            return {"ok": False, "error": "empty reset output"}
        data = json.loads(out)
        if not isinstance(data, dict):
            return {"ok": False, "error": "invalid reset output json"}
        return data
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
SCRIPT_APPENDIX_MIN_ITEMS = 3
SCRIPT_APPENDIX_MAX_ITEMS = 5
SCRIPT_STYLE_ADAPT_KEYS = ('ip_style_adaptation', 'tone_style_adaptation', 'visual_style_adaptation')
SCRIPT_CLAIM_KINDS = {'fact', 'opinion', 'mixed'}
SCRIPT_EVIDENCE_SOURCE_TYPES = {'market', 'news_flash', 'announcement', 'hotlist', 'inference', 'user_judgement'}
SCRIPT_FACT_ROLES = {'argument_1', 'argument_2', 'argument_3', 'argument', 'turn', 'scene', 'conflict', 'result', 'action'}

def _schema_error(code: str, path: str, message: str, **extra: Any) -> dict[str, Any]:
    err = {
        'code': code,
        'path': path,
        'message': message,
    }
    err.update(extra)
    return err


def _emit_payload_schema_errors(stage: str, errors: list[dict[str, Any]]) -> None:
    if not errors:
        return
    emit_error(
        'payload',
        'PAYLOAD_SCHEMA_INVALID',
        f'{stage} payload 存在 {len(errors)} 个 schema 问题；请一次性修复 errors[] 后重试。',
        stage=stage,
        errors=errors,
        hint='可先执行：python3 skills/streamy-content-gen/scripts/draft_manager.py schema --stage outline_refining|script_refining 查看最小通过模板。',
    )


def _outline_min_schema() -> dict[str, Any]:
    return {
        'title': '标题',
        'structure_template': 'standard',
        'hook': {
            'text': '开场钩子，一句话说明为什么现在值得看',
            'duration_sec': 5,
        },
        'points': [
            {
                'order': 1,
                'role': 'argument',
                'headline': '第一条核心论据',
                'evidence': '来源/指标/时间范围',
                'production_hint': '中景口播+高亮关键数字',
                'duration_sec': 12,
            },
            {
                'order': 2,
                'role': 'turn',
                'headline': '反差或转折',
                'evidence': '来源/指标/时间范围',
                'production_hint': '切近景，黄底字幕提示转折',
                'duration_sec': 12,
            },
            {
                'order': 3,
                'role': 'action',
                'headline': '观众可观察的方法',
                'evidence': '来源/指标/时间范围',
                'production_hint': '三分屏列指标，停留2秒',
                'duration_sec': 12,
            },
        ],
        'cta': {
            'type': 'comment_reply',
            'headline': '评论区领取资料或继续追问',
        },
        'total_duration_sec': 60,
        'compliance_preview': {
            'checked_rules': ['no_specific_stock', 'data_with_timeframe', 'action_stops_at_method'],
            'warnings': [],
        },
        'display_markdown': '──── 大纲 #<DID> ────\n[Hook · 5s] 开场钩子\n[制作提示] 中景口播+高亮关键数字\n────',
    }


def _script_min_schema() -> dict[str, Any]:
    return {
        'draft_id': '<DID>',
        'title': '标题',
        'duration_sec': 60,
        'structure_template': 'standard',
        'segments': [
            {
                'time': '0:00-0:05',
                'role': 'hook',
                'say': '开场钩子，直接说清这一条为什么值得听。',
                'visual': ['贴纸:重点'],
                'cta_hint': None,
            },
            {
                'time': '0:05-0:22',
                'role': 'argument_1',
                'say': '第一条事实论据，必须带时间范围、数据口径或来源。',
                'claim_kind': 'fact',
                'evidence_source_type': 'market',
                'evidence_source_ref': '来源/指标/时间范围',
                'visual': ['配图:关键数据卡'],
                'cta_hint': None,
            },
            {
                'time': '0:22-0:42',
                'role': 'turn',
                'say': '转折或解释：为什么这条信息不能只看表面。',
                'claim_kind': 'mixed',
                'evidence_source_type': 'news_flash',
                'evidence_source_ref': '来源/发布时间/标题',
                'visual': ['特效:转场'],
                'cta_hint': None,
            },
            {
                'time': '0:42-0:55',
                'role': 'action',
                'say': '给观众一个观察方法，不给具体买卖建议。',
                'claim_kind': 'opinion',
                'visual': ['配图:观察指标清单'],
                'cta_hint': None,
            },
            {
                'time': '0:55-1:00',
                'role': 'cta',
                'say': '想看完整清单，评论区回复关键词。',
                'visual': ['动作:手指评论区'],
                'cta_hint': None,
            },
        ],
        'cta': {
            'type': 'comment_reply',
            'position': 'ending',
            'phrasing': '想看完整清单，评论区回复关键词',
        },
        'production_appendix': {
            'camera_shots': ['Hook近景推入', '论据段中景留右侧图表位', '转折段轻推镜'],
            'stickers_effects': ['Hook加重点贴纸', '数据出现时加箭头', 'CTA加评论区箭头'],
            'visual_assets': ['关键数据卡', '来源截图或摘要卡', '观察指标三联表'],
            'host_actions': ['Hook停顿半秒', '讲三点时手势计数', 'CTA指向评论区'],
        },
        'compliance': {
            'status': 'pending',
            'warnings': [],
        },
        'source': {
            'topic': '主题',
            'data_sources': ['来源/指标/时间范围'],
        },
    }


def _validate_outline_schema(body: dict[str, Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    points = body.get('points')
    if not isinstance(points, list) or not points:
        errors.append(_schema_error('OUTLINE_POINTS_MISSING', 'points', 'outline_refining payload 缺少 points[] 或为空。'))
        return errors
    for idx, point in enumerate(points):
        path = f'points[{idx}]'
        if not isinstance(point, dict):
            errors.append(_schema_error('OUTLINE_POINT_INVALID', path, f'{path} 不是 JSON object。'))
            continue
        hint = point.get('production_hint')
        if not isinstance(hint, str) or not hint.strip():
            errors.append(_schema_error('OUTLINE_PRODUCTION_HINT_REQUIRED', f'{path}.production_hint', f'{path} 缺少 production_hint（轻量制作提示）。'))
            continue
        normalized = hint.strip()
        if len(normalized) > MAX_OUTLINE_PRODUCTION_HINT_LEN:
            errors.append(_schema_error('OUTLINE_PRODUCTION_HINT_TOO_LONG', f'{path}.production_hint', f'{path}.production_hint 过长（>{MAX_OUTLINE_PRODUCTION_HINT_LEN} 字）。', got=normalized))
    return errors


def _validate_script_appendix_schema(body: dict[str, Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    appendix = body.get('production_appendix')
    if not isinstance(appendix, dict):
        errors.append(_schema_error('SCRIPT_APPENDIX_REQUIRED', 'production_appendix', 'script_refining payload 缺少 production_appendix（详细制作附录）。'))
        return errors
    for section in SCRIPT_APPENDIX_SECTIONS:
        rows = appendix.get(section)
        path = f'production_appendix.{section}'
        if not isinstance(rows, list):
            errors.append(_schema_error('SCRIPT_APPENDIX_SECTION_REQUIRED', path, f'{path} 必须是数组。'))
            continue
        if not (SCRIPT_APPENDIX_MIN_ITEMS <= len(rows) <= SCRIPT_APPENDIX_MAX_ITEMS):
            errors.append(_schema_error('SCRIPT_APPENDIX_ITEMS_INVALID', path, f'{path} 条目数必须为 {SCRIPT_APPENDIX_MIN_ITEMS}-{SCRIPT_APPENDIX_MAX_ITEMS}。', got=len(rows)))
        for idx, row in enumerate(rows):
            if not isinstance(row, str) or not row.strip():
                errors.append(_schema_error('SCRIPT_APPENDIX_ITEM_INVALID', f'{path}[{idx}]', f'{path}[{idx}] 必须是非空字符串。'))
    return errors


def _validate_script_fact_opinion_schema(body: dict[str, Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    segments = body.get('segments')
    if not isinstance(segments, list) or not segments:
        errors.append(_schema_error('SCRIPT_SEGMENTS_MISSING', 'segments', 'script_refining payload 缺少 segments[] 或为空。'))
        return errors
    for idx, seg in enumerate(segments):
        path = f'segments[{idx}]'
        if not isinstance(seg, dict):
            errors.append(_schema_error('SCRIPT_SEGMENT_INVALID', path, f'{path} 不是 JSON object。'))
            continue
        role = str(seg.get('role') or '').strip()
        claim_kind = str(seg.get('claim_kind') or '').strip().lower()
        if role in SCRIPT_FACT_ROLES:
            if claim_kind not in SCRIPT_CLAIM_KINDS:
                errors.append(_schema_error('SCRIPT_CLAIM_KIND_REQUIRED', f'{path}.claim_kind', f'{path} 缺少 claim_kind，必须为 fact/opinion/mixed。'))
                continue
            source_type = str(seg.get('evidence_source_type') or '').strip()
            if claim_kind in ('fact', 'mixed'):
                if source_type not in SCRIPT_EVIDENCE_SOURCE_TYPES:
                    errors.append(_schema_error('SCRIPT_EVIDENCE_SOURCE_TYPE_REQUIRED', f'{path}.evidence_source_type', f'{path} 为 {claim_kind} 时，evidence_source_type 必须为 {sorted(SCRIPT_EVIDENCE_SOURCE_TYPES)} 之一。'))
                source_ref = str(seg.get('evidence_source_ref') or '').strip()
                if not source_ref:
                    errors.append(_schema_error('SCRIPT_EVIDENCE_SOURCE_REF_REQUIRED', f'{path}.evidence_source_ref', f'{path} 为 {claim_kind} 时，evidence_source_ref 不能为空。'))
    return errors


def _validate_script_renderer_schema(body: dict[str, Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    if not isinstance(body.get('draft_id'), str) or not str(body.get('draft_id') or '').strip():
        errors.append(_schema_error('SCRIPT_SCHEMA_MISSING_FIELD', 'draft_id', 'script.json 缺 draft_id。'))
    duration = body.get('duration_sec')
    if not isinstance(duration, (int, float)) or duration <= 0:
        errors.append(_schema_error('SCRIPT_SCHEMA_MISSING_FIELD', 'duration_sec', 'script.json 缺 duration_sec（或非正数）。'))
    segments = body.get('segments')
    if not isinstance(segments, list):
        errors.append(_schema_error('SCRIPT_SCHEMA_MISSING_FIELD', 'segments', 'script.json 缺 segments[]（必须是数组）。'))
        return errors
    if not segments:
        errors.append(_schema_error('SCRIPT_SCHEMA_SEGMENTS_EMPTY', 'segments', 'script.json.segments[] 为空，无法渲染。'))
        return errors
    for idx, seg in enumerate(segments):
        path = f'segments[{idx}]'
        if not isinstance(seg, dict):
            errors.append(_schema_error('SCRIPT_SCHEMA_SEGMENT_INVALID', path, f'{path} 不是 JSON object。'))
            continue
        for field, msg in (('time', '缺 time（格式 M:SS-M:SS）'), ('role', '缺 role'), ('say', '缺 say（口播文案）')):
            if not isinstance(seg.get(field), str) or not str(seg.get(field) or '').strip():
                errors.append(_schema_error('SCRIPT_SCHEMA_SEGMENT_INVALID', f'{path}.{field}', f'{path} {msg}。'))
    return errors


def _validate_script_style_adaptation_schema(body: dict[str, Any]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    if not str(body.get('user_style_context') or '').strip():
        return errors
    adapt = body.get('production_style_adaptation')
    if not isinstance(adapt, dict):
        return [_schema_error('SCRIPT_STYLE_ADAPTATION_REQUIRED', 'production_style_adaptation', '存在 user_style_context 时，必须提供 production_style_adaptation。')]
    for k in SCRIPT_STYLE_ADAPT_KEYS:
        if not str(adapt.get(k) or '').strip():
            errors.append(_schema_error('SCRIPT_STYLE_ADAPTATION_FIELD_REQUIRED', f'production_style_adaptation.{k}', f'production_style_adaptation.{k} 不能为空。'))
    return errors


def _validate_stage_payload_schema(stage: str, body: dict[str, Any]) -> list[dict[str, Any]]:
    if stage == STAGE_OUTLINE:
        return _validate_outline_schema(body)
    if stage == STAGE_SCRIPT:
        errors: list[dict[str, Any]] = []
        errors.extend(_validate_script_renderer_schema(body))
        errors.extend(_validate_script_appendix_schema(body))
        errors.extend(_validate_script_fact_opinion_schema(body))
        errors.extend(_validate_script_style_adaptation_schema(body))
        return errors
    return []

def _forward_artifact_files(target_stage: str) -> list[str]:
    '''返回目标阶段之后所有阶段的产物文件名（rewind 时待清理）。'''
    idx = STAGE_ORDER.index(target_stage)
    files = []
    for later_stage in STAGE_ORDER[idx + 1:]:
        cfg = STAGE_ARTIFACTS[later_stage]
        files.append(cfg['json'])
        if not cfg['md']:
            continue
        files.append(cfg['md'])
    return files

def _extract_topic_preview(
    stage: str,
    body: dict[str, Any],
    current: Any,
    *,
    is_rewind: bool,
) -> Any:
    '''从 payload 推导 meta.topic 预览字符串（**v0.1.3 P2-H 重定义**）。'''
    if stage != STAGE_TOPIC:
        return current
    title = body.get('title')
    topic = body.get('topic')
    explicit = title or topic
    if explicit:
        return explicit
    ctx = body.get('context_used')
    if isinstance(ctx, dict):
        ctx_topic = ctx.get('topic')
        if ctx_topic:
            return ctx_topic
    candidates = body.get('candidates')
    if isinstance(candidates, list) and candidates:
        first = candidates[0]
        if isinstance(first, dict) and first.get('title'):
            return first['title']
    if is_rewind:
        return None
    return current


def _assert_topic_candidates_schema(body: dict[str, Any]) -> None:
    """P0 guard: topic_picking 必须携带 thesis + 3 条 evidence。"""
    candidates = body.get('candidates')
    if not isinstance(candidates, list) or not candidates:
        emit_error(
            'payload',
            'TOPIC_CANDIDATES_MISSING',
            'topic_picking payload 缺少 candidates[] 或为空。',
        )
    for idx, c in enumerate(candidates, start=1):
        if not isinstance(c, dict):
            emit_error('payload', 'TOPIC_CANDIDATE_INVALID', f'candidates[{idx}] 不是 JSON object。')
        thesis = c.get('thesis')
        if not isinstance(thesis, str) or not thesis.strip():
            emit_error(
                'payload',
                'TOPIC_THESIS_REQUIRED',
                f'candidates[{idx}] 缺少 thesis（核心论点）。',
            )
        ev = c.get('evidence')
        if not isinstance(ev, list) or len(ev) != 3:
            emit_error(
                'payload',
                'TOPIC_EVIDENCE_REQUIRED',
                f'candidates[{idx}] evidence 必须固定 3 条。',
            )
        for j, row in enumerate(ev, start=1):
            if not isinstance(row, dict):
                emit_error('payload', 'TOPIC_EVIDENCE_ITEM_INVALID', f'candidates[{idx}].evidence[{j}] 不是 JSON object。')
            point = row.get('point')
            source_type = row.get('source_type')
            source_ref = row.get('source_ref')
            conf = str(row.get('confidence') or '').strip().lower()
            if not isinstance(point, str) or not point.strip():
                emit_error('payload', 'TOPIC_EVIDENCE_POINT_REQUIRED', f'candidates[{idx}].evidence[{j}].point 不能为空。')
            if not isinstance(source_type, str) or not source_type.strip():
                emit_error('payload', 'TOPIC_EVIDENCE_SOURCE_TYPE_REQUIRED', f'candidates[{idx}].evidence[{j}].source_type 不能为空。')
            if not isinstance(source_ref, str) or not source_ref.strip():
                emit_error('payload', 'TOPIC_EVIDENCE_SOURCE_REF_REQUIRED', f'candidates[{idx}].evidence[{j}].source_ref 不能为空。')
            if conf not in VALID_EVIDENCE_CONFIDENCE:
                emit_error(
                    'payload',
                    'TOPIC_EVIDENCE_CONFIDENCE_INVALID',
                    f'candidates[{idx}].evidence[{j}].confidence 必须是 high/medium/low。',
                    got=conf or None,
                )


def _assert_outline_schema(body: dict[str, Any]) -> None:
    """T3 guard: outline 每段必须有轻量制作提示。"""
    _emit_payload_schema_errors(STAGE_OUTLINE, _validate_outline_schema(body))


def _assert_script_appendix_schema(body: dict[str, Any]) -> None:
    """T4 guard: script 附录必须包含固定 4 块，且每块 3-5 条。"""
    _emit_payload_schema_errors(STAGE_SCRIPT, _validate_script_appendix_schema(body))


def _assert_script_fact_opinion_schema(body: dict[str, Any]) -> None:
    """T6 guard: script 段落需标注事实/观点，并给最小证据来源类型。"""
    _emit_payload_schema_errors(STAGE_SCRIPT, _validate_script_fact_opinion_schema(body))


def _assert_script_style_adaptation(body: dict[str, Any]) -> None:
    """T8 guard: 有 user_style_context 时，附录需给出结构化风格适配说明。"""
    _emit_payload_schema_errors(STAGE_SCRIPT, _validate_script_style_adaptation_schema(body))

def _load_meta_or_fail(user_id: str, draft_id: str) -> tuple[Path, dict[str, Any]]:
    draft_dir = get_active_draft_dir(user_id, draft_id)
    meta_path = draft_dir / 'meta.json'
    if not meta_path.exists():
        emit_error('draft', 'NOT_FOUND', f'''Draft #{draft_id} 在 user={user_id} 下不存在或已归档。''', draft_id = draft_id, user_id = user_id)
    meta = read_json(meta_path, default = { })
    if not isinstance(meta, dict):
        emit_error('io', 'META_CORRUPT', f'''meta.json 结构异常：{meta_path}''', path = str(meta_path))
    return (meta_path, meta)

def _load_meta_for_doctor(user_id: str, draft_id: str, *, include_archive: bool, since_days: int) -> tuple[Path, dict[str, Any], str]:
    draft_dir = get_active_draft_dir(user_id, draft_id)
    meta_path = draft_dir / 'meta.json'
    if meta_path.exists():
        meta = read_json(meta_path, default = { })
        if not isinstance(meta, dict):
            emit_error('io', 'META_CORRUPT', f'''meta.json 结构异常：{meta_path}''', path = str(meta_path))
        return (meta_path, meta, 'active')
    if include_archive:
        archive_root = get_archive_root()
        today = datetime.now(CST).date()
        cutoff = today - timedelta(days=since_days)
        for date_dir in sorted(archive_root.iterdir(), reverse=True) if archive_root.is_dir() else []:
            if not date_dir.is_dir():
                continue
            try:
                d = datetime.strptime(date_dir.name, '%Y-%m-%d').date()
            except ValueError:
                continue
            if d < cutoff:
                continue
            candidate = date_dir / user_id / draft_id / 'meta.json'
            if candidate.exists():
                meta = read_json(candidate, default = { })
                if not isinstance(meta, dict):
                    emit_error('io', 'META_CORRUPT', f'''meta.json 结构异常：{candidate}''', path = str(candidate))
                return (candidate, meta, 'archive')
    emit_error(
        'draft',
        'NOT_FOUND',
        f'''Draft #{draft_id} 在 user={user_id} 下不存在。''',
        draft_id = draft_id,
        user_id = user_id,
        include_archive = include_archive,
        archive_window_days = since_days if include_archive else None,
        hint = '若要诊断已归档稿，使用 `draft_manager.py doctor --draft <DID> --include-archive --since-days N`。',
    )

def _summarize_draft(user_id: str, draft_id: str) -> dict[str, Any]:
    '''读取单个 active draft 的摘要信息。'''
    draft_dir = get_active_draft_dir(user_id, draft_id)
    meta_path = draft_dir / 'meta.json'
    if not meta_path.exists():
        return {
            'draft_id': draft_id,
            'exists': False,
            'reason': 'meta.json missing (index 与目录不一致)' }
    meta = read_json(meta_path, default = { })
    return {
        'draft_id': meta.get('draft_id', draft_id),
        'stage': meta.get('stage'),
        'topic': meta.get('topic'),
        'style_id': meta.get('style_id'),
        'created_at': meta.get('created_at'),
        'last_updated': meta.get('last_updated') }

def _collect_archived_drafts(user_id: str, since_days: int) -> list[dict[str, Any]]:
    '''扫 archive/YYYY-MM-DD/{uid}/ 下最近 N 天的归档 draft 元信息。'''
    archive_root = get_archive_root()
    if not archive_root.is_dir():
        return []
    today = datetime.now(CST).date()
    cutoff = today - timedelta(days=since_days)
    out: list[dict[str, Any]] = []
    for date_dir in sorted(archive_root.iterdir()):
        if not date_dir.is_dir():
            continue
        try:
            d = datetime.strptime(date_dir.name, '%Y-%m-%d').date()
        except ValueError:
            continue
        if d < cutoff:
            continue
        user_dir = date_dir / user_id
        if not user_dir.is_dir():
            continue
        for draft_dir in sorted(user_dir.iterdir()):
            if not draft_dir.is_dir():
                continue
            meta_path = draft_dir / 'meta.json'
            if not meta_path.exists():
                continue
            meta = read_json(meta_path, default=None)
            if not isinstance(meta, dict):
                continue
            out.append({
                'draft_id': meta.get('draft_id', draft_dir.name),
                'stage': meta.get('stage'),
                'topic': meta.get('topic'),
                'created_at': meta.get('created_at'),
                'last_updated': meta.get('last_updated'),
                'finalized_at': meta.get('finalized_at'),
                'dropped_at': meta.get('dropped_at'),
                'drop_reason': meta.get('drop_reason'),
                'archive_date': date_dir.name,
                'archive_path': str(draft_dir) })
    out.sort(key = lambda x: x.get('last_updated') or '', reverse = True)
    return out

def cmd_create(args: argparse.Namespace) -> None:
    user_id = get_user_id()
    ts = now_iso()
    index = read_index()
    user_entry = ensure_user_entry(index, user_id)
    existing = set(user_entry.get('active_drafts', []))
    draft_id = gen_draft_id(existing)
    draft_dir = get_active_draft_dir(user_id, draft_id)
    draft_dir.mkdir(parents = True, exist_ok = True)
    topic = args.topic
    meta = {
        'draft_id': draft_id,
        'user_id': user_id,
        'stage': STAGE_TOPIC,
        'topic': topic,
        'style_id': getattr(args, 'style_id', None) or None,
        'created_at': ts,
        'last_updated': ts,
        'finalized_at': None,
        'dropped_at': None,
        'drop_reason': None }
    write_json_atomic(draft_dir / 'meta.json', meta)
    note = topic or ''
    write_json_atomic(draft_dir / 'history.json', [
        {
            'ts': ts,
            'action': 'create',
            'stage': STAGE_TOPIC,
            'note': note }])
    user_entry['active_drafts'].append(draft_id)
    user_entry['focus'] = draft_id
    user_entry['last_activity'] = ts
    write_index(index)
    emit_ok('create', result = {
        'draft_id': draft_id,
        'user_id': user_id,
        'stage': STAGE_TOPIC,
        'topic': args.topic,
        'style_id': meta.get('style_id'),
        'path': str(draft_dir) }, summary = f'''已创建 Draft #{draft_id}（stage=topic_picking，已设为 focus）''')

def cmd_list(args: argparse.Namespace) -> None:
    user_id = get_user_id()
    index = read_index()
    user_entry = index.get('users', { }).get(user_id)
    ue = user_entry or { }
    active_list = ue.get('active_drafts') or [ ]
    drafts: list[dict[str, Any]] = []
    for did in active_list:
        drafts.append(_summarize_draft(user_id, did))
    focus = ue.get('focus')
    result: dict[str, Any] = { 'user_id': user_id, 'focus': focus, 'drafts': drafts }
    archived = None
    if getattr(args, 'include_archive', False):
        archived = _collect_archived_drafts(user_id, since_days = args.since_days)
        result['archived_drafts'] = archived
        result['archive_window_days'] = args.since_days
    if not drafts and not archived:
        if getattr(args, 'include_archive', False):
            summary = f'''user={user_id} 当前没有 active Draft 也无近 {args.since_days} 天归档。'''
        else:
            summary = f'''user={user_id} 当前没有 active Draft。'''
        emit_ok('list', result = result, summary = summary)
        return
    parts = [f'active {len(drafts)} 条（focus=#{focus}）']
    if archived is not None:
        parts.append(f'archived {len(archived)} 条（近 {args.since_days} 天）')
    summary = f"user={user_id}：" + '，'.join(parts) + '。'
    emit_ok('list', result = result, summary = summary)

def cmd_archive_list(args: argparse.Namespace) -> None:
    '''P0-D (v0.1.3)：列归档稿的**唯一**合法入口。'''
    user_id = get_user_id()
    archived = _collect_archived_drafts(user_id, since_days = args.since_days)
    result = {
        'user_id': user_id,
        'archive_window_days': args.since_days,
        'count': len(archived),
        'drafts': archived }
    if not archived:
        summary = f'''user={user_id} 近 {args.since_days} 天内没有归档稿（finalized/dropped）。如需更长时间窗口，带 --since-days 参数重试。'''
    else:
        summary = f'''user={user_id} 近 {args.since_days} 天内归档 {len(archived)} 条。'''
    emit_ok('archive_list', result = result, summary = summary)

def cmd_show(args: argparse.Namespace) -> None:
    user_id = get_user_id()
    draft_id = args.draft
    (meta_path, meta) = _load_meta_or_fail(user_id, draft_id)
    draft_dir = meta_path.parent
    artifacts: dict[str, Any] = { }
    for name in ('topic_candidates.json', 'candidate_evidence_pack.json', 'outline.json', 'outline.md', 'script.json', 'script.md', 'history.json'):
        p = draft_dir / name
        artifacts[name] = {
            'exists': p.exists(),
            'size': p.stat().st_size if p.exists() else 0 }
    emit_ok('show', result = {
        'meta': meta,
        'path': str(draft_dir),
        'artifacts': artifacts }, summary = f'''Draft #{draft_id} stage={meta.get('stage')} topic={meta.get('topic')!r}''')

def _history_has_update(history: Any, stage: str) -> bool:
    if not isinstance(history, list):
        return False
    for row in history:
        if not isinstance(row, dict):
            continue
        if row.get('action') == 'update' and row.get('stage') == stage:
            return True
    return False

def _artifact_info(draft_dir: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name in ('topic_candidates.json', 'candidate_evidence_pack.json', 'outline.json', 'outline.md', 'script.json', 'script.md', 'history.json'):
        p = draft_dir / name
        out[name] = {
            'exists': p.exists(),
            'size': p.stat().st_size if p.exists() else 0,
        }
    return out

def _draft_doctor_report(meta: dict[str, Any], draft_dir: Path) -> dict[str, Any]:
    history = read_json(draft_dir / 'history.json', default=[])
    artifacts = _artifact_info(draft_dir)
    issues: list[dict[str, Any]] = []

    def issue(code: str, message: str, **extra: Any) -> None:
        row = {'code': code, 'message': message}
        row.update(extra)
        issues.append(row)

    stage = str(meta.get('stage') or '')
    has_outline_update = _history_has_update(history, STAGE_OUTLINE)
    has_script_update = _history_has_update(history, STAGE_SCRIPT)

    if stage in (STAGE_OUTLINE, STAGE_SCRIPT, STAGE_FINALIZED) and not artifacts['outline.json']['exists']:
        issue(
            'OUTLINE_JSON_MISSING',
            '当前阶段已到大纲或之后，但缺少 outline.json。',
            remediation='重新用 draft_manager update --stage outline_refining --payload-file <outline.json> 落盘。',
        )
    if stage in (STAGE_SCRIPT, STAGE_FINALIZED) and not artifacts['script.json']['exists']:
        issue(
            'SCRIPT_JSON_MISSING',
            '当前阶段已到逐字稿或已定稿，但缺少 script.json。',
            remediation='重新用 draft_manager update --stage script_refining --payload-file <script.json> 落盘。',
        )
    if stage in (STAGE_OUTLINE, STAGE_SCRIPT) and not artifacts['candidate_evidence_pack.json']['exists']:
        issue(
            'EVIDENCE_PACK_MISSING',
            '当前阶段已到大纲或之后，但缺少 candidate_evidence_pack.json。',
            remediation='先用 preflight_topic.py --candidate-id <N> 生成 evidence_pack，再用 draft_manager update --set-evidence-pack-file 落盘。',
        )
    if artifacts['outline.md']['exists'] and not artifacts['outline.json']['exists']:
        issue(
            'DIRECT_WRITE_OUTLINE_MD',
            '发现 outline.md 但缺少 outline.json，疑似 Agent 直接写大纲，绕过了 production_hint 校验。',
            remediation='重新用 draft_manager update --stage outline_refining --payload-file <outline.json> 落盘。',
        )
    if artifacts['script.md']['exists'] and not artifacts['script.json']['exists']:
        issue(
            'DIRECT_WRITE_SCRIPT_MD',
            '发现 script.md 但缺少 script.json，疑似 Agent 直接写逐字稿，绕过了 production_appendix 与合规扫描。',
            remediation='重新用 draft_manager update --stage script_refining --payload-file <script.json> 落盘。',
        )
    if stage in (STAGE_OUTLINE, STAGE_SCRIPT) and not has_outline_update:
        issue(
            'MISSING_OUTLINE_UPDATE_HISTORY',
            '当前阶段已到大纲或之后，但 history.json 没有 outline_refining 的 update 记录。',
            remediation='先重新执行 outline_refining 的结构化 update。',
        )
    if stage == STAGE_SCRIPT and not has_script_update:
        issue(
            'MISSING_SCRIPT_UPDATE_HISTORY',
            '当前阶段已到逐字稿，但 history.json 没有 script_refining 的 update 记录。',
            remediation='重新执行 script_refining 的结构化 update。',
        )

    outline_json = read_json(draft_dir / 'outline.json', default=None) if artifacts['outline.json']['exists'] else None
    if isinstance(outline_json, dict):
        points = outline_json.get('points')
        if not isinstance(points, list) or not points:
            issue('OUTLINE_POINTS_MISSING', 'outline.json 缺少 points[]。')
        else:
            for idx, point in enumerate(points, start=1):
                if not isinstance(point, dict) or not str(point.get('production_hint') or '').strip():
                    issue(
                        'OUTLINE_PRODUCTION_HINT_MISSING',
                        f'outline.json points[{idx}] 缺少 production_hint。',
                    )

    script_json = read_json(draft_dir / 'script.json', default=None) if artifacts['script.json']['exists'] else None
    if isinstance(script_json, dict):
        appendix = script_json.get('production_appendix')
        if not isinstance(appendix, dict):
            issue('SCRIPT_APPENDIX_MISSING', 'script.json 缺少 production_appendix。')
        else:
            for section in SCRIPT_APPENDIX_SECTIONS:
                rows = appendix.get(section)
                if not isinstance(rows, list) or not (SCRIPT_APPENDIX_MIN_ITEMS <= len(rows) <= SCRIPT_APPENDIX_MAX_ITEMS):
                    issue(
                        'SCRIPT_APPENDIX_SECTION_INVALID',
                        f'production_appendix.{section} 必须是 {SCRIPT_APPENDIX_MIN_ITEMS}-{SCRIPT_APPENDIX_MAX_ITEMS} 条。',
                    )

    return {
        'healthy': len(issues) == 0,
        'stage': stage,
        'artifacts': artifacts,
        'history_update': {
            STAGE_OUTLINE: has_outline_update,
            STAGE_SCRIPT: has_script_update,
        },
        'issues': issues,
    }

def _assert_upstream_integrity_for_update(stage: str, meta: dict[str, Any], draft_dir: Path, draft_id: str) -> None:
    if stage != STAGE_SCRIPT:
        return
    report = _draft_doctor_report(meta, draft_dir)
    blocking = [
        x for x in report['issues']
        if x.get('code') in {
            'DIRECT_WRITE_OUTLINE_MD',
            'EVIDENCE_PACK_MISSING',
            'MISSING_OUTLINE_UPDATE_HISTORY',
            'OUTLINE_JSON_MISSING',
            'OUTLINE_POINTS_MISSING',
            'OUTLINE_PRODUCTION_HINT_MISSING',
        }
    ]
    if blocking:
        emit_error(
            'workflow',
            'DRAFT_INCONSISTENT_DIRECT_WRITE',
            '当前 Draft 的大纲阶段疑似被直接写盘或缺少 production_hint，禁止继续生成逐字稿。',
            draft_id=draft_id,
            issues=blocking,
            hint='先执行 `draft_manager.py doctor --draft <DID>` 查看问题，再用 `draft_manager.py update --stage outline_refining --payload-file <outline.json>` 重新落盘大纲。',
        )

def _assert_ready_to_finalize(meta: dict[str, Any], draft_dir: Path, draft_id: str) -> None:
    if meta.get('stage') != STAGE_SCRIPT:
        emit_error(
            'stage',
            'FINALIZE_STAGE_NOT_READY',
            '禁止在未完成 script_refining 的状态下定稿归档。',
            draft_id=draft_id,
            current_stage=meta.get('stage'),
            expected_stage=STAGE_SCRIPT,
            hint='先用 `draft_manager.py update --stage script_refining --payload-file <script.json>` 生成结构化逐字稿。',
        )
    report = _draft_doctor_report(meta, draft_dir)
    blocking = [
        x for x in report['issues']
        if x.get('code') in {
            'DIRECT_WRITE_SCRIPT_MD',
            'EVIDENCE_PACK_MISSING',
            'MISSING_SCRIPT_UPDATE_HISTORY',
            'SCRIPT_JSON_MISSING',
            'SCRIPT_APPENDIX_MISSING',
            'SCRIPT_APPENDIX_SECTION_INVALID',
        }
    ]
    if blocking:
        emit_error(
            'workflow',
            'DRAFT_INCONSISTENT_BEFORE_FINALIZE',
            '当前 Draft 的逐字稿阶段缺少结构化 update 或制作附录，禁止定稿归档。',
            draft_id=draft_id,
            issues=blocking,
            hint='先用 `draft_manager.py update --stage script_refining --payload-file <script.json>` 重新落盘逐字稿，确保 production_appendix 四块存在。',
        )

def cmd_doctor(args: argparse.Namespace) -> None:
    user_id = get_user_id()
    draft_id = args.draft
    (meta_path, meta, location) = _load_meta_for_doctor(
        user_id,
        draft_id,
        include_archive=getattr(args, 'include_archive', False),
        since_days=getattr(args, 'since_days', 30),
    )
    draft_dir = meta_path.parent
    report = _draft_doctor_report(meta, draft_dir)
    summary = (
        f'Draft #{draft_id} 结构健康。'
        if report['healthy']
        else f"Draft #{draft_id} 发现 {len(report['issues'])} 个结构问题，请按 issues[].remediation 修复。"
    )
    emit_ok('doctor', result={
        'draft_id': draft_id,
        'user_id': user_id,
        'location': location,
        'meta': meta,
        'path': str(draft_dir),
        **report,
    }, summary=summary)

def cmd_switch(args: argparse.Namespace) -> None:
    user_id = get_user_id()
    draft_id = args.draft
    index = read_index()
    user_entry = ensure_user_entry(index, user_id)
    if draft_id not in user_entry.get('active_drafts', []):
        emit_error('draft', 'NOT_FOUND', f'''#{draft_id} 不在 user={user_id} 的 active 列表中。''', draft_id = draft_id, user_id = user_id, active_drafts = user_entry.get('active_drafts', []))
    user_entry['focus'] = draft_id
    user_entry['last_activity'] = now_iso()
    write_index(index)
    emit_ok('switch', result = {
        'user_id': user_id,
        'focus': draft_id }, summary = f'''已将 focus 切到 #{draft_id}。''')

def _cmd_update_set_chosen(args: argparse.Namespace) -> None:
    '''P0-C (v0.1.3)：原子 patch topic_candidates.json.chosen。'''
    user_id = get_user_id()
    draft_id = args.draft
    n = args.set_chosen
    (meta_path, meta) = _load_meta_or_fail(user_id, draft_id)
    draft_dir = meta_path.parent
    if meta.get('stage') in (STAGE_FINALIZED, STAGE_DROPPED):
        emit_error('draft', 'ALREADY_CLOSED', f'''Draft #{draft_id} 已 {meta.get('stage')}，不能再 update。''', draft_id = draft_id)
    if meta.get('stage') != STAGE_TOPIC:
        emit_error('draft', 'SET_CHOSEN_WRONG_STAGE', f'''--set-chosen 只能在 topic_picking 阶段使用；当前 stage={meta.get('stage')}。如需换主题方向，请先走 rewind（update --stage topic_picking --payload-file ...）。''', draft_id = draft_id, current_stage = meta.get('stage'))
    topic_json_path = draft_dir / STAGE_ARTIFACTS[STAGE_TOPIC]['json']
    tc = read_json(topic_json_path, default = None)
    if not isinstance(tc, dict):
        emit_error('draft', 'TOPIC_CANDIDATES_MISSING', f'''topic_candidates.json 不存在或不是 JSON object：{topic_json_path}''', draft_id = draft_id, path = str(topic_json_path))
    candidates = tc.get('candidates')
    if not (isinstance(candidates, list) and candidates):
        emit_error('draft', 'TOPIC_CANDIDATES_EMPTY', 'topic_candidates.json.candidates 为空或非数组，无法 set-chosen。', draft_id = draft_id)
    if n < 1 or n > len(candidates):
        emit_error('payload', 'SET_CHOSEN_OUT_OF_RANGE', f'''--set-chosen={n} 越界：当前候选共 {len(candidates)} 条（合法 1~{len(candidates)}）。''', draft_id = draft_id, total = len(candidates))
    ts = now_iso()
    old_chosen = tc.get('chosen')
    tc['chosen'] = n
    write_json_atomic(topic_json_path, tc)
    ch = candidates[n - 1] if isinstance(candidates[n - 1], dict) else { }
    if isinstance(ch, dict):
        chosen_title = ch.get('title') or ch.get('topic')
    else:
        chosen_title = None
    if chosen_title:
        meta['topic'] = chosen_title
    meta['last_updated'] = ts
    write_json_atomic(meta_path, meta)
    history_path = draft_dir / 'history.json'
    history = read_json(history_path, default = [])
    if not isinstance(history, list):
        history = []
    history.append({
        'ts': ts,
        'action': 'set_chosen',
        'stage': STAGE_TOPIC,
        'chosen': n,
        'prev_chosen': old_chosen,
        'chosen_title': chosen_title,
        'note': args.edit_note })
    write_json_atomic(history_path, history)
    index = read_index()
    user_entry = ensure_user_entry(index, user_id)
    if draft_id in user_entry.get('active_drafts', []):
        user_entry['focus'] = draft_id
        user_entry['last_activity'] = ts
        write_index(index)
    title_bit = chosen_title or '(无 title)'
    emit_ok('update', result = {
        'draft_id': draft_id,
        'user_id': user_id,
        'stage': STAGE_TOPIC,
        'chosen': n,
        'prev_chosen': old_chosen,
        'chosen_title': chosen_title,
        'topic': meta.get('topic'),
        'style_id': meta.get('style_id'),
        'edit_note': args.edit_note }, summary = f'''Draft #{draft_id} 已选定第 {n} 个主题候选（{title_bit}，之前 chosen={old_chosen}）。''')

def _cmd_update_set_style(args: argparse.Namespace) -> None:
    '''v0.1+：原子 patch meta.style_id（可清空）。'''
    user_id = get_user_id()
    draft_id = args.draft
    (meta_path, meta) = _load_meta_or_fail(user_id, draft_id)
    if meta.get('stage') in (STAGE_FINALIZED, STAGE_DROPPED, 'finalized', 'dropped'):
        emit_error('draft', 'ALREADY_CLOSED', f'''Draft #{draft_id} 已结束，不能改 style_id。''', draft_id = draft_id)
    if getattr(args, 'clear_style', False) and getattr(args, 'set_style_id', None):
        emit_error('usage', 'STYLE_ID_CONFLICT', '不能同时使用 --set-style-id 与 --clear-style。')
    if getattr(args, 'clear_style', False):
        meta['style_id'] = None
    else:
        val = getattr(args, 'set_style_id', None)
        if not val or not str(val).strip():
            emit_error('usage', 'SET_STYLE_ID_EMPTY', '--set-style-id 需为非空 UUID 字符串。')
        meta['style_id'] = str(val).strip()
    ts = now_iso()
    meta['last_updated'] = ts
    write_json_atomic(meta_path, meta)
    history_path = meta_path.parent / 'history.json'
    history = read_json(history_path, default = [])
    if not isinstance(history, list):
        history = []
    history.append({
        'ts': ts,
        'action': 'set_style_id',
        'style_id': meta.get('style_id'),
        'note': getattr(args, 'edit_note', '') or '' })
    write_json_atomic(meta_path.parent / 'history.json', history)
    index = read_index()
    user_entry = ensure_user_entry(index, user_id)
    if draft_id in user_entry.get('active_drafts', []):
        user_entry['focus'] = draft_id
        user_entry['last_activity'] = ts
        write_index(index)
    emit_ok('update', result = {
        'draft_id': draft_id,
        'user_id': user_id,
        'stage': meta.get('stage'),
        'style_id': meta.get('style_id') }, summary = f'''已更新 Draft #{draft_id} 的 style_id={meta.get('style_id')!r}。''')

def _cmd_update_set_evidence_pack(args: argparse.Namespace) -> None:
    '''v0.2.1：原子写入所选候选方向的 evidence_pack。'''
    user_id = get_user_id()
    draft_id = args.draft
    (meta_path, meta) = _load_meta_or_fail(user_id, draft_id)
    draft_dir = meta_path.parent
    if meta.get('stage') in (STAGE_FINALIZED, STAGE_DROPPED, 'finalized', 'dropped'):
        emit_error('draft', 'ALREADY_CLOSED', f'''Draft #{draft_id} 已结束，不能写 evidence_pack。''', draft_id = draft_id)
    if meta.get('stage') not in (STAGE_TOPIC, STAGE_OUTLINE):
        emit_error('draft', 'EVIDENCE_PACK_WRONG_STAGE', f'''--set-evidence-pack-file 只能在 topic_picking 或 outline_refining 阶段使用；当前 stage={meta.get('stage')}。''', draft_id = draft_id, current_stage = meta.get('stage'))
    topic_json_path = draft_dir / STAGE_ARTIFACTS[STAGE_TOPIC]['json']
    tc = read_json(topic_json_path, default=None)
    if not isinstance(tc, dict):
        emit_error('draft', 'TOPIC_CANDIDATES_MISSING', f'''topic_candidates.json 不存在或不是 JSON object：{topic_json_path}''', draft_id = draft_id, path = str(topic_json_path))
    chosen = tc.get('chosen')
    if not isinstance(chosen, int):
        emit_error('draft', 'EVIDENCE_PACK_REQUIRES_CHOSEN', '写入 evidence_pack 前必须先用 --set-chosen N 记录用户选择的候选。', draft_id = draft_id)
    payload_path = Path(args.set_evidence_pack_file)
    if not payload_path.exists():
        emit_error('io', 'EVIDENCE_PACK_NOT_FOUND', f'''evidence_pack 文件不存在：{payload_path}''', path = str(payload_path))
    payload = read_json(payload_path, default=None)
    if not isinstance(payload, dict):
        emit_error('io', 'EVIDENCE_PACK_INVALID', f'''evidence_pack 文件必须是 JSON object：{payload_path}''', path = str(payload_path))
    pack = payload.get('evidence_pack') if isinstance(payload.get('evidence_pack'), dict) else payload
    if not isinstance(pack, dict):
        emit_error('payload', 'EVIDENCE_PACK_MISSING', 'payload 缺少 evidence_pack object。')
    pack_index = pack.get('candidate_index')
    if pack_index is not None and int(pack_index) != chosen:
        emit_error('payload', 'EVIDENCE_PACK_CANDIDATE_MISMATCH', f'''evidence_pack.candidate_index={pack_index} 与 topic_candidates.chosen={chosen} 不一致。''', draft_id = draft_id, chosen = chosen, candidate_index = pack_index)
    if not pack.get('candidate_title') or not isinstance(pack.get('core_facts'), list):
        emit_error('payload', 'EVIDENCE_PACK_SCHEMA_INVALID', 'evidence_pack 必须包含 candidate_title 与 core_facts[]。')
    ts = now_iso()
    target = draft_dir / 'candidate_evidence_pack.json'
    write_json_atomic(target, pack)
    meta['evidence_pack_candidate'] = chosen
    meta['last_updated'] = ts
    write_json_atomic(meta_path, meta)
    history_path = draft_dir / 'history.json'
    history = read_json(history_path, default=[])
    if not isinstance(history, list):
        history = []
    history.append({
        'ts': ts,
        'action': 'set_evidence_pack',
        'stage': STAGE_TOPIC,
        'candidate': chosen,
        'candidate_title': pack.get('candidate_title'),
        'note': args.edit_note,
        'written': ['candidate_evidence_pack.json'],
    })
    write_json_atomic(history_path, history)
    emit_ok('update', result={
        'draft_id': draft_id,
        'user_id': user_id,
        'stage': meta.get('stage'),
        'chosen': chosen,
        'evidence_pack_candidate': chosen,
        'written': ['candidate_evidence_pack.json'],
    }, summary=f'''Draft #{draft_id} 已写入候选 {chosen} 的方向证据包。''')

def _assert_style_bound_before_generation(stage: str, meta: dict[str, Any], draft_id: str) -> None:
    """P0 guard: outline/script 生成前必须先完成 user-style 选择。"""
    if stage not in (STAGE_OUTLINE, STAGE_SCRIPT):
        return
    if str(meta.get('style_id') or '').strip():
        return
    emit_error(
        'workflow',
        'STYLE_ID_REQUIRED_BEFORE_OUTLINE',
        '生成大纲/逐字稿前必须先询问并绑定 user-style；请先调用 user-style-manager list 展示可选风格，再用 draft_manager update --set-style-id <UUID> 绑定。不得用默认风格或直接写文件绕过该门禁。',
        draft_id=draft_id,
        stage=stage,
        hint='先执行：python3 skills/user-style-manager/scripts/style_cli.py list；用户选择后执行：python3 skills/streamy-content-gen/scripts/draft_manager.py update --draft <DID> --set-style-id <UUID>',
    )

def _assert_evidence_pack_before_outline(stage: str, meta: dict[str, Any], draft_dir: Path, draft_id: str) -> None:
    if stage != STAGE_OUTLINE:
        return
    pack_path = draft_dir / 'candidate_evidence_pack.json'
    pack = read_json(pack_path, default=None)
    if not isinstance(pack, dict):
        emit_error(
            'workflow',
            'EVIDENCE_PACK_REQUIRED_BEFORE_OUTLINE',
            '生成大纲前必须先展示并落盘所选候选方向的 evidence_pack；不得从选题直接跳到风格/大纲。',
            draft_id=draft_id,
            hint='先执行 preflight_topic.py --candidate-id <N> --topic-payload-file <topic_payload.json> --snapshot-path <snapshot.json>，再用 draft_manager.py update --set-evidence-pack-file <evidence_pack.json> 落盘。',
        )
    chosen = meta.get('evidence_pack_candidate')
    if chosen is None:
        return
    pack_index = pack.get('candidate_index')
    if pack_index is not None and int(pack_index) != int(chosen):
        emit_error(
            'workflow',
            'EVIDENCE_PACK_CANDIDATE_MISMATCH',
            '已落盘 evidence_pack 与 meta.evidence_pack_candidate 不一致。',
            draft_id=draft_id,
            meta_candidate=chosen,
            pack_candidate=pack_index,
        )

def cmd_update(args: argparse.Namespace) -> None:
    want_style = bool(
        getattr(args, 'set_style_id', None) is not None
        or getattr(args, 'clear_style', False)
    )
    want_evidence_pack = bool(getattr(args, 'set_evidence_pack_file', None))
    if args.set_chosen is not None:
        if args.stage is not None or args.payload_file is not None or want_style or want_evidence_pack or getattr(args, 'validate_only', False):
            emit_error('usage', 'SET_CHOSEN_CONFLICTS', '--set-chosen 与 --stage/--payload-file/--set-style-id/--clear-style/--set-evidence-pack-file/--validate-only 互斥，请分次调用。')
        _cmd_update_set_chosen(args)
        return
    if want_style:
        if args.stage is not None or args.payload_file is not None or args.set_chosen is not None or want_evidence_pack or getattr(args, 'validate_only', False):
            emit_error('usage', 'SET_STYLE_CONFLICTS', '--set-style-id/--clear-style 与 --stage/--payload-file/--set-chosen/--set-evidence-pack-file/--validate-only 互斥。')
        _cmd_update_set_style(args)
        return
    if want_evidence_pack:
        if args.stage is not None or args.payload_file is not None or args.set_chosen is not None or getattr(args, 'validate_only', False):
            emit_error('usage', 'SET_EVIDENCE_PACK_CONFLICTS', '--set-evidence-pack-file 与 --stage/--payload-file/--set-chosen/--set-style-id/--clear-style/--validate-only 互斥。')
        _cmd_update_set_evidence_pack(args)
        return
    if args.stage is None or args.payload_file is None:
        emit_error('usage', 'UPDATE_ARGS_MISSING', 'update 须：① --stage+--payload-file ② 或 --set-chosen ③ 或 --set-style-id ④ 或 --clear-style ⑤ 或 --set-evidence-pack-file')
    user_id = get_user_id()
    draft_id = args.draft
    stage = args.stage
    ts = now_iso()
    (meta_path, meta) = _load_meta_or_fail(user_id, draft_id)
    draft_dir = meta_path.parent
    if meta.get('stage') in ('finalized', 'dropped'):
        emit_error('draft', 'ALREADY_CLOSED', f'''Draft #{draft_id} 已 {meta.get('stage')}，不能再 update。''', draft_id = draft_id)
    assert_stage_transition(str(meta.get('stage') or ''), str(stage))
    _assert_evidence_pack_before_outline(stage, meta, draft_dir, draft_id)
    _assert_style_bound_before_generation(stage, meta, draft_id)
    _assert_upstream_integrity_for_update(str(stage), meta, draft_dir, draft_id)
    payload_path = Path(args.payload_file)
    if not payload_path.exists():
        emit_error('io', 'PAYLOAD_NOT_FOUND', f'''payload-file 不存在：{payload_path}''', path = str(payload_path))
    payload = read_json(payload_path, default = None)
    if payload is None or not isinstance(payload, dict):
        emit_error('io', 'PAYLOAD_INVALID', f'''payload-file 必须是 JSON object，当前类型：{type(payload).__name__}''', path = str(payload_path))
    artifact_cfg = STAGE_ARTIFACTS[stage]
    display_md = payload.get('display_markdown')
    body = {k: v for k, v in payload.items() if k != 'display_markdown'}
    if stage == STAGE_TOPIC:
        _assert_topic_candidates_schema(body)
    # T5: auto-inject archive-driven style context for outline/script when style is bound.
    style_id = meta.get('style_id')
    if stage in (STAGE_OUTLINE, STAGE_SCRIPT) and style_id and not str(body.get('user_style_context') or '').strip():
        workspace_root = Path(__file__).parent.parent.parent
        style_cli = workspace_root / 'skills' / 'user-style-manager' / 'scripts' / 'style_cli.py'
        if style_cli.exists():
            try:
                cp = subprocess.run(
                    [
                        sys.executable,
                        str(style_cli),
                        'get-context',
                        '--style-id',
                        str(style_id),
                        '--format',
                        'json',
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    cwd=str(workspace_root),
                )
                if cp.returncode == 0 and (cp.stdout or '').strip():
                    ctx_obj = json.loads(cp.stdout)
                    if isinstance(ctx_obj, dict) and ctx_obj.get('ok') and isinstance(ctx_obj.get('context_markdown'), str):
                        body['user_style_context'] = ctx_obj['context_markdown']
            except Exception:  # noqa: BLE001
                pass
    deprecation_warnings: list[dict[str, str]] = []
    if stage == STAGE_SCRIPT and isinstance(display_md, str) and display_md.strip():
        deprecation_warnings.append({
            'type': 'deprecated_field_ignored',
            'field': 'display_markdown',
            'since': 'v0.1.3',
            'reason': 'script.md now auto-rendered from segments[] by draft_manager. Remove this field from your payload to save tokens.' })
    _emit_payload_schema_errors(stage, _validate_stage_payload_schema(stage, body))
    md_text: str | None = None
    if stage == STAGE_SCRIPT:
        try:
            md_text = render_script_md(body)
        except ScriptRenderError as e:
            emit_error('payload', e.code, e.message, hint = e.hint, renderer_version = RENDERER_VERSION)
            return
    if getattr(args, 'validate_only', False):
        planned_files = [artifact_cfg['json']]
        if artifact_cfg['md']:
            planned_files.append(artifact_cfg['md'])
        emit_ok('update', result={
            'draft_id': draft_id,
            'user_id': user_id,
            'stage': str(stage),
            'prev_stage': meta.get('stage'),
            'valid': True,
            'validate_only': True,
            'would_write': planned_files,
            'style_context_auto_injected': bool(
                stage in (STAGE_OUTLINE, STAGE_SCRIPT)
                and style_id
                and str(body.get('user_style_context') or '').strip()
                and not str(payload.get('user_style_context') or '').strip()
            ),
            'deprecation_warnings': deprecation_warnings,
        }, summary=f'Draft #{draft_id} 的 {stage} payload 校验通过（validate-only，未写盘）。')
        return
    json_target = draft_dir / artifact_cfg['json']
    write_json_atomic(json_target, body)
    written = [str(json_target.relative_to(draft_dir))]
    md_target_name = artifact_cfg['md']
    if stage == STAGE_SCRIPT:
        if deprecation_warnings:
            print(
                '[draft_manager:WARN] payload.display_markdown 在 script_refining 阶段已废弃，已忽略（v0.1.3 起由工具从 segments[] 自动渲染 script.md）。',
                file = sys.stderr,
            )
        md_target = draft_dir / md_target_name
        assert md_target_name
        assert md_text is not None
        write_text_atomic(md_target, md_text)
        written.append(str(md_target.relative_to(draft_dir)))
    else:
        if md_target_name and isinstance(display_md, str) and display_md.strip():
            md_target = draft_dir / md_target_name
            write_text_atomic(md_target, display_md)
            written.append(str(md_target.relative_to(draft_dir)))
    prev_stage = meta.get('stage')
    is_rewind = (
        prev_stage in STAGE_ORDER
        and stage in STAGE_ORDER
        and STAGE_ORDER.index(str(prev_stage)) > STAGE_ORDER.index(stage)
    )
    cleaned: list[str] = []
    if is_rewind:
        for fname in _forward_artifact_files(stage):
            fpath = draft_dir / fname
            if fpath.exists():
                fpath.unlink()
                cleaned.append(fname)
    new_topic = _extract_topic_preview(
        str(stage), body, meta.get('topic'), is_rewind = is_rewind,
    )
    meta['stage'] = str(stage)
    meta['topic'] = new_topic
    meta['last_updated'] = ts
    write_json_atomic(meta_path, meta)
    history_path = draft_dir / 'history.json'
    history = read_json(history_path, default=[])
    if not isinstance(history, list):
        history = []
    history_entry: dict[str, Any] = {
        'ts': ts,
        'action': 'update',
        'stage': str(stage),
        'prev_stage': prev_stage,
        'note': args.edit_note,
        'written': written,
    }
    if cleaned:
        history_entry['cleaned_forward'] = cleaned
    history.append(history_entry)
    write_json_atomic(history_path, history)
    index = read_index()
    user_entry = ensure_user_entry(index, user_id)
    if draft_id in user_entry.get('active_drafts', []):
        user_entry['focus'] = draft_id
        user_entry['last_activity'] = ts
    write_index(index)
    compliance_info: dict[str, Any] | None = None
    if stage == STAGE_SCRIPT:
        from lite_compliance_scan import (
            DEFAULT_BLACKLIST,
        )
        from lite_compliance_scan import run as compliance_run
        try:
            scan_result = compliance_run(
                from_draft = draft_id, script_file = None, text = None,
                blacklist_path = DEFAULT_BLACKLIST, write_back = True,
            )
            compliance_info = {
                'status': scan_result['status'],
                'warnings_count': scan_result['warnings_count'],
                'warnings': scan_result['warnings'],
                'scanner_version': scan_result.get('write_back', { }).get('scanner_version'),
                'scanned_at': scan_result.get('write_back', { }).get('scanned_at'),
            }
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001 — mirror bytecode broad handler
            compliance_info = {
                'status': 'scan_failed',
                'error': f'''{type(e).__name__}: {e}''',
            }
            err = compliance_info['error']
            print(
                f'[draft_manager:ERROR] 内嵌合规扫描失败：{err}，'
                'script.json 已落盘但 compliance 字段未刷新；请 Agent 手动跑 lite_compliance_scan.py --from-draft <DID> --write-back 补救。',
                file = sys.stderr,
            )
    if prev_stage != stage:
        stage_transition = f'''{prev_stage!s} → {stage!s}'''
    else:
        stage_transition = f'''stay at {stage!s}'''
    if cleaned:
        summary_extra = f'，清理 forward {len(cleaned)} 个'
    else:
        summary_extra = ''
    if compliance_info:
        st = compliance_info.get('status')
        if st == 'pass':
            summary_extra += '，合规 🟢 通过'
        elif st == 'warn':
            summary_extra += f'''，合规 🟡 命中 {compliance_info.get('warnings_count', 0)} 处'''
        elif st == 'scan_failed':
            summary_extra += '，⚠️ 合规扫描失败（见 stderr）'
    result_payload: dict[str, Any] = {
        'draft_id': draft_id,
        'user_id': user_id,
        'stage': str(stage),
        'prev_stage': prev_stage,
        'topic': new_topic,
        'style_id': meta.get('style_id'),
        'written': written,
        'cleaned_forward': cleaned,
        'edit_note': args.edit_note,
    }
    if compliance_info is not None:
        result_payload['compliance'] = compliance_info
    if deprecation_warnings:
        result_payload['deprecation_warnings'] = deprecation_warnings
    emit_ok('update', result = result_payload, summary = f'''Draft #{draft_id} 已更新（{stage_transition}），落盘 {len(written)} 个文件{summary_extra}。''')

def _archive_draft(
    user_id: str, draft_id: str, *, final_stage: str, action: str, note: str, extra_meta: dict[str, Any],
) -> dict[str, Any]:
    ts = now_iso()
    (meta_path, meta) = _load_meta_or_fail(user_id, draft_id)
    draft_dir = meta_path.parent
    if meta.get('stage') in (STAGE_FINALIZED, STAGE_DROPPED):
        emit_error('draft', 'ALREADY_CLOSED', f'''Draft #{draft_id} 已 {meta.get('stage')}。''', draft_id = draft_id)
    prev_stage = meta.get('stage')
    meta['stage'] = final_stage
    meta['last_updated'] = ts
    meta.update(extra_meta)
    write_json_atomic(meta_path, meta)
    history_path = draft_dir / 'history.json'
    history = read_json(history_path, default=[])
    if not isinstance(history, list):
        history = []
    history.append({
        'ts': ts,
        'action': action,
        'stage': final_stage,
        'prev_stage': prev_stage,
        'note': note,
    })
    write_json_atomic(history_path, history)
    archive_target = get_archive_root() / today_date_str() / user_id / draft_id
    if archive_target.exists():
        emit_error('draft', 'ARCHIVE_CONFLICT', f'''归档目标已存在：{archive_target}（同一天同 user 同 ID 重复归档？）''', path = str(archive_target))
    archive_target.parent.mkdir(parents = True, exist_ok = True)
    draft_dir.rename(archive_target)
    user_active_dir = draft_dir.parent
    if user_active_dir.exists() and not any(user_active_dir.iterdir()):
        user_active_dir.rmdir()
    index = read_index()
    user_entry = ensure_user_entry(index, user_id)
    active = list(user_entry.get('active_drafts', []))
    if draft_id in active:
        active.remove(draft_id)
    if user_entry.get('focus') == draft_id:
        user_entry['focus'] = active[-1] if active else None
    user_entry['active_drafts'] = active
    user_entry['last_activity'] = ts
    write_index(index)
    return {
        'draft_id': draft_id,
        'user_id': user_id,
        'prev_stage': prev_stage,
        'stage': final_stage,
        'archive_path': str(archive_target),
        'new_focus': user_entry.get('focus'),
        'remaining_active': active,
    }

def _try_auto_refine(
    user_id: str, draft_id: str, meta: dict[str, Any], draft_dir: Path,
) -> dict[str, Any] | None:
    '''
    尝试自动 refine style：
    1. 检查是否有 style_id
    2. 检查 script.md 是否存在
    3. 调用 style_cli.py refine
    返回 refine 结果（成功/失败），失败时返回 None，不影响流程
    '''
    style_id = meta.get('style_id')
    if not style_id:
        return None
    
    script_md = draft_dir / 'script.md'
    if not script_md.exists():
        return None
    
    # 定位 style_cli.py
    workspace_root = Path(__file__).parent.parent.parent
    style_cli = workspace_root / 'skills' / 'user-style-manager' / 'scripts' / 'style_cli.py'
    if not style_cli.exists():
        print(f'[draft_manager:WARNING] style_cli.py 不存在，跳过 auto-refine：{style_cli}', file=sys.stderr)
        return None
    
    try:
        result = subprocess.run(
            [sys.executable, str(style_cli), 'refine', '--style-id', style_id, '--text-file', str(script_md)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        
        if result.returncode == 0:
            refine_output = json.loads(result.stdout)
            return {
                'ok': True,
                'style_id': style_id,
                'refine_count': refine_output.get('refine_count'),
                'style_name': refine_output.get('style_name'),
            }
        else:
            print(f'[draft_manager:WARNING] auto-refine 失败：{result.stderr}', file=sys.stderr)
            return {
                'ok': False,
                'style_id': style_id,
                'error': result.stderr,
            }
    except Exception as e:
        print(f'[draft_manager:WARNING] auto-refine 异常：{type(e).__name__}: {e}', file=sys.stderr)
        return {
            'ok': False,
            'style_id': style_id,
            'error': str(e),
        }

def cmd_finalize(args: argparse.Namespace) -> None:
    user_id = get_user_id()
    draft_id = args.draft
    
    # 如果启用 auto-refine，先尝试 refine（在归档前，因为归档后 draft_dir 会被移动）
    refine_result = None
    (meta_path, meta) = _load_meta_or_fail(user_id, draft_id)
    draft_dir = meta_path.parent
    _assert_ready_to_finalize(meta, draft_dir, draft_id)
    if getattr(args, 'auto_refine', False):
        try:
            refine_result = _try_auto_refine(user_id, draft_id, meta, draft_dir)
        except Exception as e:
            print(f'[draft_manager:WARNING] auto-refine 准备失败：{type(e).__name__}: {e}', file=sys.stderr)
    
    # 执行归档
    result = _archive_draft(
        user_id = user_id, draft_id = draft_id, final_stage = STAGE_FINALIZED, action = 'finalize', note = '', extra_meta = { 'finalized_at': now_iso() },
    )

    reset_result: dict[str, Any] | None = None
    auto_reset_enabled = bool(getattr(args, 'min_context_reset', False)) or _truthy_env(
        "STREAM_GEN_MIN_CONTEXT_RESET_AFTER_FINALIZE", default=False
    )
    if auto_reset_enabled:
        reset_result = _run_min_context_reset()
    
    # 构建 summary
    summary_parts = [f'''Draft #{result['draft_id']} 已定稿归档到 {result['archive_path']}，剩余 active: {result['remaining_active']}，focus=#{result['new_focus']}。
✅ 上下文优化建议：本次内容生成已完成，建议输入 `/reset` 开启新会话，避免上下文累积浪费token。''']
    if refine_result:
        if refine_result['ok']:
            summary_parts.append(f'''✅ 已自动 refine 风格 #{refine_result['style_id']}（{refine_result['style_name']}），refine_count={refine_result['refine_count']}。''')
        else:
            summary_parts.append(f'''⚠️ 尝试 auto-refine 风格 #{refine_result['style_id']} 失败（见 stderr）。''')
    if auto_reset_enabled:
        if reset_result and reset_result.get("ok"):
            summary_parts.append(
                f'''✅ 已执行最小上下文清理：归档 {reset_result.get("to_archive_count", 0)} 个历史会话文件，保留最近会话。'''
            )
        else:
            summary_parts.append("⚠️ 最小上下文清理执行失败（见 result.min_context_reset）。")

    emit_ok(
        'finalize',
        result={**result, 'refine_result': refine_result, 'min_context_reset': reset_result},
        summary=' '.join(summary_parts),
    )

def cmd_drop(args: argparse.Namespace) -> None:
    reason = args.reason if args.reason else None
    result = _archive_draft(
        get_user_id(), args.draft, final_stage = STAGE_DROPPED, action = 'drop', note = args.reason, extra_meta = {
        'dropped_at': now_iso(), 'drop_reason': reason } )
    emit_ok('drop', result = result, summary = f'''Draft #{result['draft_id']} 已放弃归档到 {result['archive_path']}，剩余 active: {result['remaining_active']}，focus=#{result['new_focus']}。''')

def cmd_schema(args: argparse.Namespace) -> None:
    stage = args.stage
    if stage == STAGE_OUTLINE:
        template = _outline_min_schema()
    elif stage == STAGE_SCRIPT:
        template = _script_min_schema()
    else:
        emit_error('usage', 'SCHEMA_STAGE_UNSUPPORTED', f'暂不支持 stage={stage} 的 schema 模板。', stage=stage)
        return
    emit_ok(
        'schema',
        result={
            'stage': stage,
            'schema_kind': 'minimal_pass_template',
            'template': template,
            'usage': f'生成 payload 后先执行：python3 skills/streamy-content-gen/scripts/draft_manager.py update --draft <DID> --stage {stage} --payload-file <payload.json> --validate-only --json',
        },
        summary=f'{stage} 最小通过模板已返回；few-shot 不在此模板内。',
    )

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog = 'draft_manager', description = 'streamy-content-gen Draft 生命周期管理')
    common = argparse.ArgumentParser(add_help = False)
    common.add_argument('--json', action = 'store_true', help = 'JSON 输出（默认开启）')
    sub = parser.add_subparsers(dest = 'command', required = True)
    p_create = sub.add_parser('create', help = '创建新 Draft', parents = [ common ])
    p_create.add_argument('--topic', default = None, help = '初始主题（可选）')
    p_create.add_argument('--style-id', default = None, dest = 'style_id', help = '绑定的 user-style-manager style_id（UUID，可选）')
    p_create.set_defaults(func = cmd_create)
    p_list = sub.add_parser('list', help = '列出当前 user 的 active Draft', parents = [ common ])
    p_list.add_argument('--include-archive', action = 'store_true', help = '同时列出归档的 Draft（finalized / dropped）')
    p_list.add_argument('--since-days', type = int, default = 30, help = '归档扫描的时间窗口（天），默认 30（仅在 --include-archive 时生效）')
    p_list.set_defaults(func = cmd_list)
    p_archive = sub.add_parser('archive-list', help = '列归档稿（finalized/dropped）· v0.1.3 起为唯一合法入口', parents = [ common ])
    p_archive.add_argument('--since-days', type = int, default = 30, help = '扫描时间窗口（天），默认 30')
    p_archive.set_defaults(func = cmd_archive_list)
    p_show = sub.add_parser('show', help = '展示 Draft 状态', parents = [ common ])
    p_show.add_argument('--draft', required = True, help = 'Draft ID，例如 A3F')
    p_show.set_defaults(func = cmd_show)
    p_doctor = sub.add_parser('doctor', help = '诊断 Draft 是否绕过 draft_manager / 缺少制作指导结构', parents = [ common ])
    p_doctor.add_argument('--draft', required = True, help = 'Draft ID，例如 A3F')
    p_doctor.add_argument('--include-archive', action = 'store_true', help = '若 active 未找到，则扫描归档稿')
    p_doctor.add_argument('--since-days', type = int, default = 30, help = '归档扫描窗口（天），默认 30')
    p_doctor.set_defaults(func = cmd_doctor)
    p_switch = sub.add_parser('switch', help = '切换焦点 Draft', parents = [ common ])
    p_switch.add_argument('--draft', required = True, help = 'Draft ID')
    p_switch.set_defaults(func = cmd_switch)
    p_update = sub.add_parser('update', help = '更新 Draft 产物', parents = [ common ])
    p_update.add_argument('--draft', required = True)
    p_update.add_argument('--stage', choices = [ 'topic_picking', 'outline_refining', 'script_refining' ], help = 'stage + --payload-file 成对使用：整阶段产物更新')
    p_update.add_argument('--payload-file', help = '产物 JSON 文件路径（与 --stage 成对）')
    p_update.add_argument('--set-chosen', type = int, default = None, metavar = 'N', help = '原子 patch：把 topic_candidates.json.chosen 设为 N（1-based，互斥于 --stage/--payload-file，仅允许在 topic_picking 阶段使用）。v0.1.3 起这是唯一合法的 chosen 修改入口。')
    p_update.add_argument('--set-evidence-pack-file', default = None, dest = 'set_evidence_pack_file', help = '原子 patch：写入所选候选方向的 candidate_evidence_pack.json（需先 --set-chosen N）')
    p_update.add_argument('--set-style-id', default = None, dest = 'set_style_id', help = '原子 patch：设 meta.style_id（UUID，与整阶段更新 / set-chosen 互斥）')
    p_update.add_argument('--clear-style', action = 'store_true', help = '清空 meta.style_id')
    p_update.add_argument('--edit-note', default = '', help = '人改描述，写入 history.json')
    p_update.add_argument('--validate-only', action = 'store_true', help = '只执行门禁与 payload/schema/渲染校验，不写入 draft 文件')
    p_update.set_defaults(func = cmd_update)
    p_schema = sub.add_parser('schema', help = '输出 outline/script 最小通过 payload 模板', parents = [ common ])
    p_schema.add_argument('--stage', required = True, choices = [ 'outline_refining', 'script_refining' ], help = '要查看模板的阶段')
    p_schema.set_defaults(func = cmd_schema)
    p_finalize = sub.add_parser('finalize', help = '定稿归档', parents = [ common ])
    p_finalize.add_argument('--draft', required = True)
    p_finalize.add_argument('--auto-refine', action = 'store_true', help = '定稿后自动 refine 绑定的 style（如果有 style_id 且 script.md 存在）')
    p_finalize.add_argument('--min-context-reset', action='store_true', help='定稿归档后执行最小上下文清理（仅会话层，不动 drafts/memory）')
    p_finalize.set_defaults(func = cmd_finalize)
    p_drop = sub.add_parser('drop', help = '放弃归档', parents = [ common ])
    p_drop.add_argument('--draft', required = True)
    p_drop.add_argument('--reason', default = '', help = '可选 drop 原因')
    p_drop.set_defaults(func = cmd_drop)
    return parser

def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)

if __name__ == '__main__':
    main(sys.argv[1:])
