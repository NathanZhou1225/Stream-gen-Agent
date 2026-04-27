# Source Generated with Decompyle++
# File: draft_manager.cpython-312.pyc (Python 3.12)

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
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from _common import CST, STAGE_DROPPED, STAGE_FINALIZED, STAGE_OUTLINE, STAGE_SCRIPT, STAGE_TOPIC, emit_error, emit_ok, ensure_user_entry, gen_draft_id, get_active_draft_dir, get_archive_root, get_user_id, now_iso, read_index, read_json, today_date_str, write_index, write_json_atomic, write_text_atomic
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

def _forward_artifact_files(target_stage = None):
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


def _extract_topic_preview(stage = None, body = None, current = None, *, is_rewind):
    '''从 payload 推导 meta.topic 预览字符串（**v0.1.3 P2-H 重定义**）。

    新语义：**meta.topic 仅代表"高阶主题方向"，只在 topic_picking 阶段刷新**。
    进入 outline_refining / script_refining 后，**payload 顶层的 title 不再覆盖 meta.topic**
    （v0.1.2 观察到 "B: 断舍离清单" set-chosen 后被大纲标题 "xxx 三件套" 覆盖，
    语义与 topic_candidates.json.chosen 的 title 不一致）。

    具体规则：
      - stage == topic_picking：
          1. payload 顶层 topic / title（显式）
          2. context_used.topic
          3. candidates[0].title
          4. rewind 场景且以上都无 → 返回 None（清空旧 topic 避免误导）
          5. 否则保持 current
      - stage != topic_picking：
          直接返回 current（**不读 payload.title/topic**）
          topic 只能由 set-chosen / 重新 update topic_picking 来改。
    '''
    if stage != STAGE_TOPIC:
        return current
    if not None.get('title'):
        None.get('title')
    explicit = body.get('topic')
    if explicit:
        return explicit
    ctx = None.get('context_used')
    if isinstance(ctx, dict):
        ctx_topic = ctx.get('topic')
        if ctx_topic:
            return ctx_topic
        candidates = None.get('candidates')
        if isinstance(candidates, list) and candidates:
            first = candidates[0]
            if isinstance(first, dict) and first.get('title'):
                return first['title']
            if None:
                return None
    return current


def _load_meta_or_fail(user_id = None, draft_id = None):
    draft_dir = get_active_draft_dir(user_id, draft_id)
    meta_path = draft_dir / 'meta.json'
    if not meta_path.exists():
        emit_error('draft', 'NOT_FOUND', f'''Draft #{draft_id} 在 user={user_id} 下不存在或已归档。''', draft_id = draft_id, user_id = user_id)
    meta = read_json(meta_path, default = { })
    if not isinstance(meta, dict):
        emit_error('io', 'META_CORRUPT', f'''meta.json 结构异常：{meta_path}''', path = str(meta_path))
    return (meta_path, meta)


def _summarize_draft(user_id = None, draft_id = None):
    '''读取单个 active draft 的摘要信息。'''
    draft_dir = get_active_draft_dir(user_id, draft_id)
    meta_path = draft_dir / 'meta.json'
    if not meta_path.exists():
        return {
            'draft_id': draft_id,
            'exists': False,
            'reason': 'meta.json missing (index 与目录不一致)' }
    meta = None(meta_path, default = { })
    return {
        'draft_id': meta.get('draft_id', draft_id),
        'stage': meta.get('stage'),
        'topic': meta.get('topic'),
        'created_at': meta.get('created_at'),
        'last_updated': meta.get('last_updated') }


def _collect_archived_drafts(user_id = None, since_days = None):
    '''扫 archive/YYYY-MM-DD/{uid}/ 下最近 N 天的归档 draft 元信息。

    v0.1.2 Issue #3 修复：让 Agent 列归档清单不再靠 find/ls 脑补 topic。

    返回列表按 last_updated 倒序排（最近归档的排前）。
    '''
    archive_root = get_archive_root()
    if not archive_root.is_dir():
        return []
    today = None.now(CST).date()
    cutoff = today - timedelta(days = since_days)
    out = []
    for date_dir in sorted(archive_root.iterdir()):
        if not date_dir.is_dir():
            continue
        d = datetime.strptime(date_dir.name, '%Y-%m-%d').date()
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
            meta = read_json(meta_path, default = None)
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
    out.sort(key = (lambda x: if not x.get('last_updated'):
x.get('last_updated')''), reverse = True)
    return out
    except ValueError:
        continue


def cmd_create(args = None):
    user_id = get_user_id()
    ts = now_iso()
    index = read_index()
    user_entry = ensure_user_entry(index, user_id)
    existing = set(user_entry.get('active_drafts', []))
    draft_id = gen_draft_id(existing)
    draft_dir = get_active_draft_dir(user_id, draft_id)
    draft_dir.mkdir(parents = True, exist_ok = True)
    meta = {
        'draft_id': draft_id,
        'user_id': user_id,
        'stage': STAGE_TOPIC,
        'topic': args.topic,
        'created_at': ts,
        'last_updated': ts,
        'finalized_at': None,
        'dropped_at': None,
        'drop_reason': None }
    write_json_atomic(draft_dir / 'meta.json', meta)
    if not args.topic:
        args.topic
    write_json_atomic(draft_dir / 'history.json', [
        {
            'ts': ts,
            'action': 'create',
            'stage': STAGE_TOPIC,
            'note': '' }])
    user_entry['active_drafts'].append(draft_id)
    user_entry['focus'] = draft_id
    user_entry['last_activity'] = ts
    write_index(index)
    emit_ok('create', result = {
        'draft_id': draft_id,
        'user_id': user_id,
        'stage': STAGE_TOPIC,
        'topic': args.topic,
        'path': str(draft_dir) }, summary = f'''已创建 Draft #{draft_id}（stage=topic_picking，已设为 focus）''')


def cmd_list(args = None):
    user_id = get_user_id()
    index = read_index()
    user_entry = index.get('users', { }).get(user_id)
    if not user_entry:
        user_entry
    if not { }.get('active_drafts'):
        { }.get('active_drafts')
    active_list = []
# WARNING: Decompyle incomplete


def cmd_archive_list(args = None):
    '''P0-D (v0.1.3)：列归档稿的**唯一**合法入口。

    语义与 `list --include-archive` 重合但**独立存在**，目的：
      1. 给 Agent 一个专用入口词（intent.md 里绑死"归档/历史/以前做过"→ archive-list）
      2. 禁掉 Agent 凭对话记忆脑补归档稿存在（v0.1.2 路径 5 翻车的根本原因）

    Agent 侧硬话术（见 prompts/natural-language-intent.md §2.x）：
      - 用户一问"以前"/"历史"/"归档"/"之前那条"，必须先调本命令
      - **绝对禁止**用 ls / find 探盘、凭对话记忆补 draft_id
    '''
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


def cmd_show(args = None):
    user_id = get_user_id()
    draft_id = args.draft
    (meta_path, meta) = _load_meta_or_fail(user_id, draft_id)
    draft_dir = meta_path.parent
    artifacts = { }
    for name in ('topic_candidates.json', 'outline.json', 'outline.md', 'script.json', 'script.md', 'history.json'):
        p = draft_dir / name
        artifacts[name] = {
            'exists': p.exists(),
            'size': p.stat().st_size if p.exists() else 0 }
    emit_ok('show', result = {
        'meta': meta,
        'path': str(draft_dir),
        'artifacts': artifacts }, summary = f'''Draft #{draft_id} stage={meta.get('stage')} topic={meta.get('topic')!r}''')


def cmd_switch(args = None):
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


def _cmd_update_set_chosen(args = None):
    '''P0-C (v0.1.3)：原子 patch topic_candidates.json.chosen，**唯一合法入口**。

    设计要点：
        - 阶段硬约束：只允许在 topic_picking 阶段用（进 outline/script 后想换方向必须先 rewind）
        - 边界校验：n 必须 1 <= n <= len(candidates)
        - 顺带把 meta.topic 刷新到对应 candidate 的 title（在 topic_picking 阶段刷新 meta.topic 是 P2-H 允许的）
        - history 追加一条 action=set_chosen，记录 prev_chosen + new_chosen
    '''
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
    if not isinstance(candidates, list) or candidates:
        emit_error('draft', 'TOPIC_CANDIDATES_EMPTY', 'topic_candidates.json.candidates 为空或非数组，无法 set-chosen。', draft_id = draft_id)
    if n < 1 or n > len(candidates):
        emit_error('payload', 'SET_CHOSEN_OUT_OF_RANGE', f'''--set-chosen={n} 越界：当前候选共 {len(candidates)} 条（合法 1~{len(candidates)}）。''', draft_id = draft_id, total = len(candidates))
    ts = now_iso()
    old_chosen = tc.get('chosen')
    tc['chosen'] = n
    write_json_atomic(topic_json_path, tc)
    chosen_item = candidates[n - 1] if isinstance(candidates[n - 1], dict) else { }
    chosen_title = chosen_item.get('topic') if not isinstance(chosen_item, dict) or chosen_item.get('title') else None
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
    if not chosen_title:
        chosen_title
    emit_ok('update', result = {
        'draft_id': draft_id,
        'user_id': user_id,
        'stage': STAGE_TOPIC,
        'chosen': n,
        'prev_chosen': old_chosen,
        'chosen_title': chosen_title,
        'topic': meta.get('topic'),
        'edit_note': args.edit_note }, summary = f'''Draft #{draft_id} 已选定第 {n} 个主题候选（{'(无 title)'}，之前 chosen={old_chosen}）。''')


def cmd_update(args = None):
    pass
# WARNING: Decompyle incomplete


def _archive_draft(user_id = None, draft_id = None, *, final_stage, action, note, extra_meta):
    '''finalize / drop 的共用归档逻辑。

    返回 result dict（供 emit_ok 使用）。
    '''
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
    history = read_json(history_path, default = [])
    if not isinstance(history, list):
        history = []
    history.append({
        'ts': ts,
        'action': action,
        'stage': final_stage,
        'prev_stage': prev_stage,
        'note': note })
    write_json_atomic(history_path, history)
    archive_target = get_archive_root() / today_date_str() / user_id / draft_id
    if archive_target.exists():
        emit_error('draft', 'ARCHIVE_CONFLICT', f'''归档目标已存在：{archive_target}（同一天同 user 同 ID 重复归档？）''', path = str(archive_target))
    archive_target.parent.mkdir(parents = True, exist_ok = True)
    draft_dir.rename(archive_target)
    user_active_dir = draft_dir.parent
    if not user_active_dir.exists() and any(user_active_dir.iterdir()):
        user_active_dir.rmdir()
    index = read_index()
    user_entry = ensure_user_entry(index, user_id)
    active = user_entry.get('active_drafts', [])
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
        'remaining_active': active }


def cmd_finalize(args = None):
    result = _archive_draft(user_id = get_user_id(), draft_id = args.draft, final_stage = STAGE_FINALIZED, action = 'finalize', note = '', extra_meta = {
        'finalized_at': now_iso() })
    emit_ok('finalize', result = result, summary = f'''Draft #{result['draft_id']} 已定稿归档到 {result['archive_path']}，剩余 active: {result['remaining_active']}，focus=#{result['new_focus']}。''')


def cmd_drop(args = None):
    if not args.reason:
        args.reason
    result = _archive_draft(user_id = get_user_id(), draft_id = args.draft, final_stage = STAGE_DROPPED, action = 'drop', note = args.reason, extra_meta = {
        'dropped_at': now_iso(),
        'drop_reason': None })
    emit_ok('drop', result = result, summary = f'''Draft #{result['draft_id']} 已放弃归档到 {result['archive_path']}，剩余 active: {result['remaining_active']}，focus=#{result['new_focus']}。''')


def build_parser():
    parser = argparse.ArgumentParser(prog = 'draft_manager', description = 'streamy-content-gen Draft 生命周期管理')
    common = argparse.ArgumentParser(add_help = False)
    common.add_argument('--json', action = 'store_true', help = 'JSON 输出（默认开启）')
    sub = parser.add_subparsers(dest = 'command', required = True)
    p_create = sub.add_parser('create', help = '创建新 Draft', parents = [
        common])
    p_create.add_argument('--topic', default = None, help = '初始主题（可选）')
    p_create.set_defaults(func = cmd_create)
    p_list = sub.add_parser('list', help = '列出当前 user 的 active Draft', parents = [
        common])
    p_list.add_argument('--include-archive', action = 'store_true', help = '同时列出归档的 Draft（finalized / dropped）')
    p_list.add_argument('--since-days', type = int, default = 30, help = '归档扫描的时间窗口（天），默认 30（仅在 --include-archive 时生效）')
    p_list.set_defaults(func = cmd_list)
    p_archive = sub.add_parser('archive-list', help = '列归档稿（finalized/dropped）· v0.1.3 起为唯一合法入口', parents = [
        common])
    p_archive.add_argument('--since-days', type = int, default = 30, help = '扫描时间窗口（天），默认 30')
    p_archive.set_defaults(func = cmd_archive_list)
    p_show = sub.add_parser('show', help = '展示 Draft 状态', parents = [
        common])
    p_show.add_argument('--draft', required = True, help = 'Draft ID，例如 A3F')
    p_show.set_defaults(func = cmd_show)
    p_switch = sub.add_parser('switch', help = '切换焦点 Draft', parents = [
        common])
    p_switch.add_argument('--draft', required = True, help = 'Draft ID')
    p_switch.set_defaults(func = cmd_switch)
    p_update = sub.add_parser('update', help = '更新 Draft 产物', parents = [
        common])
    p_update.add_argument('--draft', required = True)
    p_update.add_argument('--stage', choices = [
        'topic_picking',
        'outline_refining',
        'script_refining'], help = 'stage + --payload-file 成对使用：整阶段产物更新')
    p_update.add_argument('--payload-file', help = '产物 JSON 文件路径（与 --stage 成对）')
    p_update.add_argument('--set-chosen', type = int, default = None, metavar = 'N', help = '原子 patch：把 topic_candidates.json.chosen 设为 N（1-based，互斥于 --stage/--payload-file，仅允许在 topic_picking 阶段使用）。v0.1.3 起这是唯一合法的 chosen 修改入口。')
    p_update.add_argument('--edit-note', default = '', help = '人改描述，写入 history.json')
    p_update.set_defaults(func = cmd_update)
    p_finalize = sub.add_parser('finalize', help = '定稿归档', parents = [
        common])
    p_finalize.add_argument('--draft', required = True)
    p_finalize.set_defaults(func = cmd_finalize)
    p_drop = sub.add_parser('drop', help = '放弃归档', parents = [
        common])
    p_drop.add_argument('--draft', required = True)
    p_drop.add_argument('--reason', default = '', help = '可选 drop 原因')
    p_drop.set_defaults(func = cmd_drop)
    return parser


def main(argv = None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)

if __name__ == '__main__':
    main(sys.argv[1:])
    return None
