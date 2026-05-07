#!/usr/bin/env python3
"""
会话健康检查工具
检测当前会话大小，提醒用户适时Reset
"""
import json
import os
from pathlib import Path

SESSION_DIR = Path("/root/.openclaw/agents/stream-gen/sessions")

def get_session_health(session_id: str = None) -> dict:
    """检查会话健康状态"""
    if not session_id:
        # 读取sessions.json获取当前活跃会话
        sessions_json = SESSION_DIR / "sessions.json"
        if sessions_json.exists():
            data = json.loads(sessions_json.read_text(encoding="utf-8"))
            for key, info in data.items():
                if info.get("sessionId"):
                    session_id = info["sessionId"]
                    break
    
    if not session_id:
        return {"error": "No active session found"}
    
    session_file = SESSION_DIR / f"{session_id}.jsonl"
    if not session_file.exists():
        return {"error": f"Session file not found: {session_id}"}
    
    # 统计消息
    msg_count = 0
    user_count = 0
    assistant_count = 0
    total_chars = 0
    
    with open(session_file, "r", encoding="utf-8") as f:
        for line in f:
            try:
                msg = json.loads(line)
                if msg.get("type") == "message":
                    message = msg.get("message", {})
                    content = message.get("content", "")
                    role = message.get("role")
                    
                    if role == "user":
                        user_count += 1
                    elif role == "assistant":
                        assistant_count += 1
                    
                    if isinstance(content, str):
                        total_chars += len(content)
                    elif isinstance(content, list):
                        for item in content:
                            if item.get("type") == "text":
                                total_chars += len(item.get("text", ""))
                            elif item.get("type") == "thinking":
                                total_chars += len(item.get("thinking", ""))
                    
                    msg_count += 1
            except:
                pass
    
    # 计算健康分
    health_score = 100
    warnings = []
    suggestions = []
    
    if user_count > 15:
        health_score -= 30
        warnings.append(f"用户消息过多: {user_count} 轮 (>15)")
        suggestions.append("建议立即 /reset 开启新会话")
    
    if total_chars > 80000:
        health_score -= 40
        warnings.append(f"上下文过大: {total_chars:,} 字符 (>80k)")
        suggestions.append("上下文过大严重浪费token，强烈建议Reset")
    
    if assistant_count > 60:
        health_score -= 20
        warnings.append(f"助手回复过多: {assistant_count} 条")
    
    return {
        "session_id": session_id,
        "file_size_kb": round(session_file.stat().st_size / 1024, 1),
        "total_messages": msg_count,
        "user_messages": user_count,
        "assistant_messages": assistant_count,
        "total_chars": total_chars,
        "estimated_tokens": total_chars // 2,
        "health_score": health_score,
        "warnings": warnings,
        "suggestions": suggestions,
        "need_reset": health_score < 70
    }

if __name__ == "__main__":
    import sys
    result = get_session_health(sys.argv[1] if len(sys.argv) > 1 else None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
