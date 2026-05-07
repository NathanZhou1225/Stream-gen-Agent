#!/usr/bin/env python3
"""Minimal callback adapter for RSSHub repair actions.

POST /rsshub/repair
Body JSON:
{
  "decision": "confirm" | "ignore",
  "sources": "news",
  "keywords": "",
  "max_items": 30
}

Optional auth header:
  X-Callback-Token: <token>
When FINANCE_RSSHUB_CALLBACK_TOKEN is set, header token must match.

Feishu card callback endpoint:
  POST /feishu/card/callback
Supports:
  - challenge handshake (returns {"challenge": ...})
  - action payload parsing (confirm/ignore)
Optional env:
  FINANCE_FEISHU_VERIFICATION_TOKEN  # validates body.token or body.header.token
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
INGEST_SCRIPT = SCRIPT_DIR / "ingest.py"


def _json_response(handler: BaseHTTPRequestHandler, code: int, payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def _require_callback_token_or_401(handler: BaseHTTPRequestHandler) -> bool:
    expected = (os.environ.get("FINANCE_RSSHUB_CALLBACK_TOKEN") or "").strip()
    provided = (handler.headers.get("X-Callback-Token") or "").strip()
    if expected and provided != expected:
        _json_response(handler, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
        return False
    return True


def _load_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any] | None:
    try:
        content_len = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        content_len = 0
    raw = handler.rfile.read(max(0, content_len))
    try:
        return json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        return None


def _extract_decision_from_feishu_payload(body: dict[str, Any]) -> str:
    # Common shapes:
    # 1) {"action":{"value":{"decision":"confirm"}}}
    # 2) {"action":{"value":{"action":"ignore"}}}
    # 3) {"action":{"value":{"key":"confirm_execute"}}}
    # 4) {"decision":"confirm"} (manual test)
    candidates: list[str] = []
    action = body.get("action") if isinstance(body, dict) else None
    if isinstance(action, dict):
        val = action.get("value")
        if isinstance(val, dict):
            for k in ("decision", "action", "key", "choice"):
                v = val.get(k)
                if isinstance(v, str):
                    candidates.append(v)
        tag = action.get("tag")
        if isinstance(tag, str):
            candidates.append(tag)
    for k in ("decision", "action"):
        v = body.get(k)
        if isinstance(v, str):
            candidates.append(v)

    blob = " ".join(candidates).strip().lower()
    if any(x in blob for x in ("confirm", "execute", "yes", "执行", "确认")):
        return "confirm"
    return "ignore"


def _check_feishu_verification_token(body: dict[str, Any]) -> bool:
    expected = (os.environ.get("FINANCE_FEISHU_VERIFICATION_TOKEN") or "").strip()
    if not expected:
        return True
    token_candidates: list[str] = []
    direct = body.get("token")
    if isinstance(direct, str):
        token_candidates.append(direct)
    header = body.get("header")
    if isinstance(header, dict):
        hv = header.get("token")
        if isinstance(hv, str):
            token_candidates.append(hv)
    return expected in token_candidates


def _run_repair_command(
    *,
    decision: str,
    sources: str,
    keywords: str,
    max_items_int: int,
    callback_token: str,
) -> tuple[bool, dict[str, Any]]:
    cmd = [
        "python3",
        str(INGEST_SCRIPT),
        "repair-rsshub",
        "--decision",
        decision,
        "--sources",
        sources,
        "--keywords",
        keywords,
        "--max-items",
        str(max_items_int),
    ]
    if callback_token:
        cmd.extend(["--token", callback_token])

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(SCRIPT_DIR.parent),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=900,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, {"ok": False, "error": "timeout"}

    if proc.returncode != 0:
        return False, {
            "ok": False,
            "error": "repair_command_failed",
            "returncode": proc.returncode,
            "stderr": (proc.stderr or "")[-1500:],
        }

    try:
        payload = json.loads(proc.stdout)
    except Exception:
        return False, {"ok": False, "error": "invalid_command_output"}
    return True, {"ok": True, "result": payload}


class CallbackHandler(BaseHTTPRequestHandler):
    server_version = "rsshub-callback/0.1"

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in {"/rsshub/repair", "/feishu/card/callback"}:
            _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return

        if not _require_callback_token_or_401(self):
            return

        body = _load_json_body(self)
        if body is None:
            _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_json"})
            return

        if self.path == "/feishu/card/callback":
            # Feishu URL verification handshake
            challenge = body.get("challenge")
            if isinstance(challenge, str) and challenge:
                _json_response(self, HTTPStatus.OK, {"challenge": challenge})
                return
            if not _check_feishu_verification_token(body):
                _json_response(self, HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "feishu_token_invalid"})
                return
            decision = _extract_decision_from_feishu_payload(body)
        else:
            decision = str(body.get("decision") or "ignore").strip().lower()

        if decision not in {"confirm", "execute", "yes", "ignore", "no"}:
            _json_response(
                self,
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "invalid_decision", "allowed": ["confirm", "ignore"]},
            )
            return

        sources = str(body.get("sources") or "news").strip() or "news"
        keywords = str(body.get("keywords") or "").strip()
        max_items = body.get("max_items", 30)
        try:
            max_items_int = int(max_items)
        except Exception:
            max_items_int = 30

        callback_token = (os.environ.get("FINANCE_RSSHUB_CALLBACK_TOKEN") or "").strip()
        ok, payload = _run_repair_command(
            decision=decision,
            sources=sources,
            keywords=keywords,
            max_items_int=max_items_int,
            callback_token=callback_token,
        )
        if not ok:
            code = HTTPStatus.INTERNAL_SERVER_ERROR
            if payload.get("error") == "timeout":
                code = HTTPStatus.GATEWAY_TIMEOUT
            _json_response(self, code, payload)
            return
        _json_response(self, HTTPStatus.OK, payload)

    def log_message(self, fmt: str, *args: object) -> None:
        # Keep stdout quiet by default.
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="RSSHub callback adapter server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), CallbackHandler)
    print(f"[rsshub-callback] listening on http://{args.host}:{args.port}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
