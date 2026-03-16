# /opt/ai/gateway/metrics.py  — Analytics, token estimation, usage tracking, feedback
import json
import time
import uuid
import sqlite3
from threading import Lock
from typing import Any, Dict, Optional

import aiosqlite
from fastapi.responses import JSONResponse

try:
    import tiktoken
except Exception:
    tiktoken = None

# -----------------------------------------------------------------------------
# Module-level config (set via configure())
# -----------------------------------------------------------------------------
_METRICS_ENABLED = True
_TOKENIZER_MODEL = ""
_ANALYTICS_DB = "/opt/ai/gateway/analytics.sqlite"

# -----------------------------------------------------------------------------
# In-memory metrics (best-effort)
# -----------------------------------------------------------------------------
_METRICS_STARTED_AT = time.time()
_METRICS_LOCK = Lock()
_METRICS: Dict[str, int] = {
    "reasoning_stripped": 0,
    "empty_response_fallbacks": 0,
    "image_requests": 0,
    "image_success": 0,
    "image_failed": 0,
    "upstream_errors": 0,
    "stream_errors": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
}


def configure(metrics_enabled: bool, tokenizer_model: str, analytics_db: str) -> None:
    global _METRICS_ENABLED, _TOKENIZER_MODEL, _ANALYTICS_DB
    _METRICS_ENABLED = metrics_enabled
    _TOKENIZER_MODEL = tokenizer_model
    _ANALYTICS_DB = analytics_db


def _metrics_inc(key: str, n: int = 1) -> None:
    if not _METRICS_ENABLED:
        return
    with _METRICS_LOCK:
        _METRICS[key] = _METRICS.get(key, 0) + n


def _metrics_snapshot() -> Dict[str, Any]:
    if not _METRICS_ENABLED:
        return {"enabled": False}
    with _METRICS_LOCK:
        data = dict(_METRICS)
    data["enabled"] = True
    data["uptime_sec"] = int(time.time() - _METRICS_STARTED_AT)
    return data


def _estimate_tokens(text: str, model: Optional[str] = None) -> int:
    if not text:
        return 0
    if tiktoken is not None:
        try:
            enc = tiktoken.encoding_for_model(model or _TOKENIZER_MODEL or "gpt-4o-mini")
        except Exception:
            try:
                enc = tiktoken.get_encoding("cl100k_base")
            except Exception:
                enc = None
        if enc is not None:
            try:
                return len(enc.encode(text))
            except Exception:
                pass
    # Heuristic fallback: ~4 chars per token
    return max(1, int(len(text) / 4))


def _extract_usage(resp_json: Dict[str, Any]) -> Dict[str, int]:
    usage = resp_json.get("usage") or {}
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    total = usage.get("total_tokens")

    # Responses API style
    if prompt is None:
        prompt = usage.get("input_tokens")
    if completion is None:
        completion = usage.get("output_tokens")

    if total is None and isinstance(prompt, int) and isinstance(completion, int):
        total = prompt + completion

    out: Dict[str, int] = {}
    if isinstance(prompt, int):
        out["prompt_tokens"] = prompt
    if isinstance(completion, int):
        out["completion_tokens"] = completion
    if isinstance(total, int):
        out["total_tokens"] = total
    return out


def _update_usage_metrics(usage: Dict[str, int]) -> None:
    if not usage:
        return
    if "prompt_tokens" in usage:
        _metrics_inc("prompt_tokens", usage["prompt_tokens"])
    if "completion_tokens" in usage:
        _metrics_inc("completion_tokens", usage["completion_tokens"])
    if "total_tokens" in usage:
        _metrics_inc("total_tokens", usage["total_tokens"])


def _attach_usage_headers(headers: Dict[str, str], usage: Dict[str, int]) -> Dict[str, str]:
    if not usage:
        return headers
    if "prompt_tokens" in usage:
        headers["X-Usage-Prompt-Tokens"] = str(usage["prompt_tokens"])
    if "completion_tokens" in usage:
        headers["X-Usage-Completion-Tokens"] = str(usage["completion_tokens"])
    if "total_tokens" in usage:
        headers["X-Usage-Total-Tokens"] = str(usage["total_tokens"])
    return headers


def _error_response(status: int, code: str, message: str, hint: Optional[str] = None, details: Optional[str] = None) -> JSONResponse:
    payload: Dict[str, Any] = {"error": {"code": code, "message": message}}
    if hint:
        payload["error"]["hint"] = hint
    if details:
        payload["error"]["details"] = details
    return JSONResponse(status_code=status, content=payload)


async def _ensure_feedback_schema() -> None:
    async with aiosqlite.connect(_ANALYTICS_DB) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS feedback(
          id TEXT PRIMARY KEY,
          ts INTEGER NOT NULL,
          user_email TEXT,
          type TEXT,
          message TEXT,
          context TEXT
        );
        """)
        await db.commit()


async def _record_feedback(user_email: Optional[str], ftype: str, message: str, context: Optional[str]) -> str:
    fid = str(uuid.uuid4())
    await _ensure_feedback_schema()
    async with aiosqlite.connect(_ANALYTICS_DB) as db:
        await db.execute(
            "INSERT INTO feedback(id, ts, user_email, type, message, context) VALUES (?,?,?,?,?,?)",
            (fid, int(time.time()), user_email, ftype, message, context),
        )
        await db.commit()
    return fid
