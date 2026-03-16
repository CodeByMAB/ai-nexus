# /opt/ai/gateway/app.py  — Gateway with SDXL + Zep memory adapter + Responses API + streaming passthrough
import asyncio
import base64
import hashlib
import json
import logging
import re
import os
import sys
import time
import uuid
import random
import sqlite3
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple, Union

import aiosqlite
import httpx

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from aux_routes import register_aux_routes
from auth_mw import RequireBearerAndMeter
from tooling_pipeline import (
    configure as configure_tooling_pipeline,
    _ensure_toolcall_finish_reason,
    _finalize_read_tool_calls,
    _has_tool_calls,
    _maybe_force_web_fetch_tool_call,
    _maybe_promote_json_to_tool_call,
    _maybe_short_circuit_read,
    _maybe_short_circuit_web_fetch,
    _normalize_tool_call_names_in_resp,
    _patch_responses_tool_output,
    _patch_tool_call_arguments,
    _recover_truncated_tool_calls,
    _strip_silent_reply,
    _strip_tool_call_content,
    _validate_and_repair_tool_calls,
    _is_silent_token,
    _is_silent_prefix,
)

import sdxl_backend
import memory_adapter
import metrics as metrics_mod
import response_normalization as resp_norm

# -----------------------------------------------------------------------------
# Core config
# -----------------------------------------------------------------------------
DB_PATH = os.environ.get("KEY_DB_PATH", "/opt/ai/keys/keys.sqlite")

# Upstream text LLM (OpenAI-compatible backend)
UPSTREAM = os.environ.get("UPSTREAM", "https://api.YOURDOMAIN.COM")

# Per-endpoint upstream overrides (optional)
CHAT_UPSTREAM = os.environ.get("CHAT_UPSTREAM", UPSTREAM)
RESPONSES_UPSTREAM = os.environ.get("RESPONSES_UPSTREAM", CHAT_UPSTREAM)
EMBEDDINGS_UPSTREAM = os.environ.get("EMBEDDINGS_UPSTREAM", UPSTREAM)

# Route /v1/responses via chat if upstream doesn't support responses
RESPONSES_VIA_CHAT = os.getenv("RESPONSES_VIA_CHAT", "1") == "1"
# Prefer native /v1/responses when available, even if RESPONSES_VIA_CHAT=1.
# Default to chat emulation because some local backends reject OpenAI-style
# responses chunks like `input_text`.
PREFER_NATIVE_RESPONSES = os.getenv("PREFER_NATIVE_RESPONSES", "0") == "1"
# Force non-stream responses fallback (collect full upstream response then synthesize SSE).
# Keep disabled by default to avoid long apparent "hangs" on large generations.
RESPONSES_STREAM_FALLBACK = os.getenv("RESPONSES_STREAM_FALLBACK", "0") == "1"

# Allowed models (enforced globally)
ALLOWED_CHAT_MODELS = set(
    m.strip() for m in os.getenv("ALLOWED_CHAT_MODELS", "gpt-oss:20b").split(",") if m.strip()
)
ALLOWED_IMAGE_MODELS = set(
    m.strip() for m in os.getenv("ALLOWED_IMAGE_MODELS", "sdxl-1.0").split(",") if m.strip()
)
ALLOWED_EMBED_MODELS = set(
    m.strip() for m in os.getenv("ALLOWED_EMBED_MODELS", "nomic-embedding").split(",") if m.strip()
)
DEFAULT_EMBED_MODEL = os.getenv("DEFAULT_EMBED_MODEL", "nomic-embedding").strip() or "nomic-embedding"
ENABLE_EMBED_FALLBACK = os.getenv("ENABLE_EMBED_FALLBACK", "1") == "1"
FALLBACK_EMBED_DIM = int(os.getenv("FALLBACK_EMBED_DIM", "1024"))

# Tool calling behavior controls
ENABLE_SHORT_CIRCUIT = os.getenv("ENABLE_SHORT_CIRCUIT", "0") == "1"  # Disable by default for natural inference
ENABLE_TOOL_HINT = os.getenv("ENABLE_TOOL_HINT", "1") == "1"  # Enable by default but can be disabled
DEBUG_TOOL_PIPELINE = os.getenv("DEBUG_TOOL_PIPELINE", "0") == "1"

# Optional token validator (not used in this file but kept for parity)
VALIDATE_URL = os.environ.get("VALIDATE_URL", "http://127.0.0.1:9090/validate")

# -----------------------------------------------------------------------------
# Local model routing (local-first, tools-aware)
# -----------------------------------------------------------------------------
DEFAULT_TEXT_MODEL = os.getenv("DEFAULT_TEXT_MODEL", "gpt-oss:20b").strip() or "gpt-oss:20b"
TOOL_TEXT_MODEL = os.getenv("TOOL_TEXT_MODEL", "").strip()  # set to a tool-capable local model id (optional)
TOOL_ROUTING = os.getenv("TOOL_ROUTING", "1") == "1"        # enable/disable tool-based routing

# -----------------------------------------------------------------------------
# SDXL backend config
# -----------------------------------------------------------------------------
SD_BACKEND = os.getenv("SD_BACKEND", "auto1111")  # "auto1111" or "comfy"
SD_URL = os.getenv("SD_URL", "http://127.0.0.1:7860")  # Auto1111 default
SD_TIMEOUT = float(os.getenv("SD_TIMEOUT", "120"))

# InvokeAI backend config
INVOKEAI_DB_PATH = os.getenv("INVOKEAI_DB_PATH", "${HOME}/invokeai/databases/invokeai.db")
INVOKEAI_QUEUE_ID = os.getenv("INVOKEAI_QUEUE_ID", "default")
INVOKEAI_POLL_INTERVAL = float(os.getenv("INVOKEAI_POLL_INTERVAL", "1.0"))
INVOKEAI_POLL_TIMEOUT = float(os.getenv("INVOKEAI_POLL_TIMEOUT", "120"))

# -----------------------------------------------------------------------------
# Memory adapter config (Zep)
# -----------------------------------------------------------------------------
MEMORY_PROVIDER = os.getenv("MEMORY_PROVIDER", "none").lower()  # "none" | "zep"
ZEP_URL = os.getenv("ZEP_URL", "http://127.0.0.1:8000")
ZEP_API_KEY = os.getenv("ZEP_API_KEY", "")

MEMORY_TOP_K = int(os.getenv("MEMORY_TOP_K", "6"))
MEMORY_CONTEXT_CHAR_BUDGET = int(os.getenv("MEMORY_CONTEXT_CHAR_BUDGET", "2800"))

# -----------------------------------------------------------------------------
# Analytics & tokenization
# -----------------------------------------------------------------------------
ANALYTICS_DB = os.getenv("ANALYTICS_DB", "/opt/ai/gateway/analytics.sqlite")
METRICS_ENABLED = os.getenv("METRICS_ENABLED", "1") == "1"
TOKENIZER_MODEL = os.getenv("TOKENIZER_MODEL", "").strip()
EMPTY_RESPONSE_TEXT = os.getenv("EMPTY_RESPONSE_TEXT", "I'm sorry, I couldn't generate a response. Please try again.")
HEARTBEAT_TOKEN = "HEARTBEAT_OK"
SILENT_REPLY_TOKEN = "NO_REPLY"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


# External request guardrails.
EXTERNAL_CHAT_MAX_TOKENS = _env_int("EXTERNAL_CHAT_MAX_TOKENS", 2048)
EXTERNAL_RESPONSES_MAX_TOKENS = _env_int("EXTERNAL_RESPONSES_MAX_TOKENS", 1024)
TOOL_MIN_MAX_TOKENS = _env_int("TOOL_MIN_MAX_TOKENS", 1024)

# -----------------------------------------------------------------------------
# Configure extracted modules
# -----------------------------------------------------------------------------
metrics_mod.configure(METRICS_ENABLED, TOKENIZER_MODEL, ANALYTICS_DB)
sdxl_backend.configure(SD_URL, SD_TIMEOUT, INVOKEAI_DB_PATH, INVOKEAI_QUEUE_ID, INVOKEAI_POLL_INTERVAL, INVOKEAI_POLL_TIMEOUT)
memory_adapter.configure(ZEP_URL, ZEP_API_KEY)
resp_norm.configure(DEFAULT_TEXT_MODEL, EMPTY_RESPONSE_TEXT)

# Re-export for use within this file
_metrics_inc = metrics_mod._metrics_inc
_metrics_snapshot = metrics_mod._metrics_snapshot
_estimate_tokens = metrics_mod._estimate_tokens
_extract_usage = metrics_mod._extract_usage
_update_usage_metrics = metrics_mod._update_usage_metrics
_attach_usage_headers = metrics_mod._attach_usage_headers
_error_response = metrics_mod._error_response
_record_feedback = metrics_mod._record_feedback

_coerce_responses_content_to_text = resp_norm._coerce_responses_content_to_text
_extract_text_from_responses_output = resp_norm._extract_text_from_responses_output
_ensure_responses_output_text_and_usage = resp_norm._ensure_responses_output_text_and_usage
_responses_from_chat_completion = resp_norm._responses_from_chat_completion
_as_sse = resp_norm._as_sse
_responses_sse_from_response = resp_norm._responses_sse_from_response
_ensure_nonempty_chat_content = resp_norm._ensure_nonempty_chat_content
_strip_reasoning_from_chunk = resp_norm._strip_reasoning_from_chunk
_strip_reasoning_from_nonstream = resp_norm._strip_reasoning_from_nonstream
_normalize_chat_response_schema = resp_norm._normalize_chat_response_schema
_make_chat_completion_from_text = resp_norm._make_chat_completion_from_text
_make_responses_from_text = resp_norm._make_responses_from_text
_strip_reasoning_from_responses_output = resp_norm._strip_reasoning_from_responses_output
_normalize_responses_output_text_blocks = resp_norm._normalize_responses_output_text_blocks
_chat_completion_from_sse = resp_norm._chat_completion_from_sse

_sdxl_generate_auto1111 = sdxl_backend._sdxl_generate_auto1111
_sdxl_generate_comfy = sdxl_backend._sdxl_generate_comfy
_invokeai_generate = sdxl_backend._invokeai_generate

zep_upsert_session = memory_adapter.zep_upsert_session
zep_search_memory = memory_adapter.zep_search_memory
zep_write_messages = memory_adapter.zep_write_messages

# -----------------------------------------------------------------------------
# App & CORS
# -----------------------------------------------------------------------------
app = FastAPI()
logger = logging.getLogger("ai-gateway")

ALLOWED_ORIGINS = [
    "http://192.168.50.212:3000",
    "http://192.168.50.212:8000",
    "http://localhost:3000",
    "http://localhost:8000",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:8000",
    "https://ai.YOURDOMAIN.COM",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "authorization",
        "content-type",
        "x-user-email",
        "x-openwebui-user-email",
        "x-openwebui-user-name",
        "x-openwebui-user-id",
        "x-openwebui-user-role",
        "x-openwebui-chat-id",
    ],
    expose_headers=["X-Process-Time"],
)

# Require bearer & metering middleware (yours)
app.add_middleware(RequireBearerAndMeter)

# -----------------------------------------------------------------------------
# DB helpers (key lookup)
# -----------------------------------------------------------------------------
async def get_user_record_by_email(email: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        sql = "SELECT * FROM api_keys WHERE client_email = ? AND is_active = 1"
        async with db.execute(sql, (email,)) as cur:
            return await cur.fetchone()

async def get_user_record_by_pg_key(pg_key: str):
    h = hashlib.sha256(pg_key.encode("utf-8")).hexdigest()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        sql = "SELECT * FROM api_keys WHERE key_hash = ? AND is_active = 1"
        async with db.execute(sql, (h,)) as cur:
            return await cur.fetchone()

def strip_auth(headers: Dict[str, str]) -> Dict[str, str]:
    # Don't forward client creds upstream
    clean = {k: v for k, v in headers.items() if k.lower() != "authorization"}
    clean.pop("x-user-email", None)
    return clean

def user_session_id_from_row(row) -> str:
    if row is None:
        return "anonymous"
    email = row.get("client_email") if isinstance(row, dict) else row["client_email"]
    if email:
        return f"session:{email}"
    rid = row.get("id") if isinstance(row, dict) else row["id"]
    return f"session:uid-{rid}"

def openwebui_chat_session_id(request: Request, row) -> str:
    """Scope memory to OpenWebUI chat id to prevent cross-chat bleed."""
    base = user_session_id_from_row(row)
    chat_id = request.headers.get("x-openwebui-chat-id")
    if not isinstance(chat_id, str):
        return base
    chat_id = chat_id.strip()
    if not chat_id:
        return base
    safe_chat_id = re.sub(r"[^A-Za-z0-9_.:-]", "-", chat_id)[:96]
    if not safe_chat_id:
        return base
    return f"{base}:chat:{safe_chat_id}"

def extract_token_from_auth_header(auth: str) -> str:
    """
    Mirror auth_mw.py behavior:
    - Bearer <token>
    - Basic <base64> where decoded may be token, token:, or user:pass (take first part)
    - Direct "pg_..." in Authorization header
    """
    if not auth:
        return ""
    a = auth.strip()
    if a.lower().startswith("bearer "):
        return a.split(" ", 1)[1].strip()
    if a.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(a.split(" ", 1)[1]).decode("utf-8")
            if ":" in decoded:
                return decoded.split(":", 1)[0]
            return decoded
        except Exception:
            return ""
    if a.startswith("pg_"):
        return a
    return ""

# -----------------------------------------------------------------------------
# Responses API helpers (OpenAI-compatible)
# -----------------------------------------------------------------------------
def _is_openwebui(request: Request) -> bool:
    return bool(
        request.headers.get("x-openwebui-user-email")
        or request.headers.get("x-openwebui-user-id")
        or request.headers.get("x-openwebui-chat-id")
    )

def _should_route_to_tool_model(payload: Dict[str, Any]) -> bool:
    if not TOOL_ROUTING:
        return False
    if not TOOL_TEXT_MODEL:
        return False
    tools = payload.get("tools")
    return isinstance(tools, list) and len(tools) > 0

def _ensure_model(payload: Dict[str, Any]) -> None:
    # Fill missing model
    if not payload.get("model"):
        payload["model"] = DEFAULT_TEXT_MODEL
    # If tools are present, route to tool model (still local)
    if _should_route_to_tool_model(payload):
        payload["model"] = TOOL_TEXT_MODEL

def _canonicalize_model_name(payload: Dict[str, Any]) -> None:
    """Map common display labels to gateway model IDs for client compatibility."""
    model = payload.get("model")
    if not isinstance(model, str):
        return
    m = model.strip()
    if not m:
        return
    aliases = {
        "ministral-3-14b (fast)": "fast",
        "ministral-3-14b": "fast",
        "mistral-small-3.2-24b (general)": "extreme",
        "mistral-small-3.2-24b": "extreme",
        "devstral-small-2-24b (code)": "code",
        "devstral-small-2-24b": "code",
    }
    mapped = aliases.get(m.lower())
    if mapped:
        payload["model"] = mapped

def _enforce_model_allowlist(payload: Dict[str, Any], allowed: set, fallback: str) -> None:
    try:
        model = payload.get("model")
        if not model or model not in allowed:
            payload["model"] = fallback
    except Exception:
        payload["model"] = fallback


def _debug_str(value: Any, max_len: int = 220) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        s = value
    else:
        try:
            s = json.dumps(value, ensure_ascii=False)
        except Exception:
            s = str(value)
    if len(s) <= max_len:
        return s
    return s[:max_len] + "...(truncated)"


def _debug_args_obj(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return {}
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _log_tool_pipeline_stage(stage: str, resp_json: Dict[str, Any], is_chat: bool, is_responses: bool) -> None:
    if not DEBUG_TOOL_PIPELINE or not isinstance(resp_json, dict):
        return
    try:
        snapshot: Dict[str, Any] = {"stage": stage}
        if is_chat:
            choices = resp_json.get("choices") or []
            c0 = choices[0] if choices and isinstance(choices[0], dict) else {}
            snapshot["finish_reason"] = c0.get("finish_reason")
            msg = c0.get("message") if isinstance(c0, dict) else {}
            tcs = msg.get("tool_calls") if isinstance(msg, dict) else None
            calls: List[Dict[str, Any]] = []
            if isinstance(tcs, list):
                for tc in tcs:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                    name = fn.get("name")
                    args_raw = fn.get("arguments")
                    args_obj = _debug_args_obj(args_raw)
                    partial = (
                        tc.get("partialJson")
                        or tc.get("partial_json")
                        or (fn.get("partialJson") if isinstance(fn, dict) else None)
                        or (fn.get("partial_json") if isinstance(fn, dict) else None)
                    )
                    calls.append({
                        "name": name,
                        "arg_keys": sorted(args_obj.keys())[:10],
                        "has_path": bool(args_obj.get("path") or args_obj.get("file_path") or args_obj.get("filepath") or args_obj.get("file")),
                        "args_raw": _debug_str(args_raw),
                        "partial": _debug_str(partial),
                    })
            snapshot["tool_calls"] = calls
        if is_responses:
            snapshot["status"] = resp_json.get("status")
            out = resp_json.get("output")
            calls = []
            if isinstance(out, list):
                for item in out:
                    if not isinstance(item, dict) or item.get("type") != "function_call":
                        continue
                    args_raw = item.get("arguments")
                    args_obj = _debug_args_obj(args_raw)
                    partial = item.get("partialJson") or item.get("partial_json")
                    calls.append({
                        "name": item.get("name"),
                        "call_status": item.get("status"),
                        "arg_keys": sorted(args_obj.keys())[:10],
                        "has_path": bool(args_obj.get("path") or args_obj.get("file_path") or args_obj.get("filepath") or args_obj.get("file")),
                        "args_raw": _debug_str(args_raw),
                        "partial": _debug_str(partial),
                    })
            snapshot["tool_calls"] = calls
        logger.warning("tool_pipeline_debug %s", _debug_str(snapshot, max_len=6000))
    except Exception:
        logger.exception("tool_pipeline_debug_failed stage=%s", stage)

def _normalize_embedding_inputs(payload: Dict[str, Any]) -> List[str]:
    raw = payload.get("input")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        out: List[str] = []
        for item in raw:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, list):
                out.append(" ".join(str(x) for x in item))
            else:
                out.append(str(item))
        return out
    return [str(raw)]

def _deterministic_embedding(text: str, dim: int) -> List[float]:
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8", "ignore")).digest()[:8], "big")
    rng = random.Random(seed)
    vec = [rng.uniform(-1.0, 1.0) for _ in range(dim)]
    norm = sum(v * v for v in vec) ** 0.5
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec

def _make_local_embedding_response(payload: Dict[str, Any]) -> Dict[str, Any]:
    model = payload.get("model") or DEFAULT_EMBED_MODEL
    inputs = _normalize_embedding_inputs(payload)
    if not inputs:
        inputs = [""]
    dim = max(8, FALLBACK_EMBED_DIM)
    data = []
    for idx, item in enumerate(inputs):
        data.append(
            {
                "object": "embedding",
                "index": idx,
                "embedding": _deterministic_embedding(item, dim),
            }
        )
    token_count = sum(max(1, _estimate_tokens(item)) for item in inputs)
    return {
        "object": "list",
        "data": data,
        "model": model,
        "usage": {
            "prompt_tokens": token_count,
            "total_tokens": token_count,
        },
    }

def _should_use_local_embedding_fallback(resp_json: Any) -> bool:
    if not isinstance(resp_json, dict):
        return False
    if isinstance(resp_json.get("data"), list):
        return False
    err = resp_json.get("error")
    if isinstance(err, dict):
        msg = str(err.get("message") or "").lower()
        if "does not support embeddings api" in msg:
            return True
        if "embedding" in msg and ("not support" in msg or "unsupported" in msg):
            return True
    return False

def _extract_last_user_from_chat_messages(messages: List[Dict[str, Any]]) -> str:
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str) and c.strip():
                return c.strip()
    return ""

def _detect_owui_background_task(text: str) -> Optional[Dict[str, Any]]:
    if not isinstance(text, str):
        return None
    markers = [
        "Suggest 3-5 relevant follow-up",
        "Generate 1-3 broad tags",
        "Generate a concise title",
        "Return ONLY JSON",
        "\"follow_ups\"",
        "\"tags\"",
        "\"title\"",
        "\"queries\"",
    ]
    if not any(m in text for m in markers):
        return None
    if "Generate a concise title" in text or "\"title\"" in text:
        return {"title": "Chat"}
    if "tags" in text or "\"tags\"" in text:
        return {"tags": []}
    if "follow-up" in text or "follow up" in text or "\"follow_ups\"" in text:
        return {"follow_ups": []}
    if "\"queries\"" in text:
        return {"queries": []}
    return {}


def _responses_input_to_messages(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Normalize Responses API 'input' into a chat-completions messages list.

    Handles all Responses API item types:
    - input: "string" -> [{"role":"user","content": "..."}]
    - input: [{"role":"user","content":"..."}] -> same
    - input: [{"role":"user","content":[{"type":"input_text","text":"..."}]}] -> extracted text
    - input: [{"type":"function_call",...}] -> assistant message with tool_calls
    - input: [{"type":"function_call_output",...}] -> tool role message
    """
    out: List[Dict[str, Any]] = []
    instructions = payload.get("instructions")
    system_prompt = payload.get("system")
    system_prompt_alt = payload.get("system_prompt")
    for sys_text in (instructions, system_prompt, system_prompt_alt):
        if isinstance(sys_text, str) and sys_text.strip():
            out.append({"role": "system", "content": sys_text.strip()})

    inp = payload.get("input")
    if inp is None and isinstance(payload.get("messages"), list):
        inp = payload.get("messages")
    if isinstance(inp, str):
        s = inp.strip()
        if s:
            out.append({"role": "user", "content": s})
        return out
    if isinstance(inp, list):
        # Buffer for grouping consecutive function_call items into one assistant message
        pending_tool_calls: List[Dict[str, Any]] = []

        def _flush_pending_tool_calls() -> None:
            if pending_tool_calls:
                out.append({"role": "assistant", "tool_calls": list(pending_tool_calls)})
                pending_tool_calls.clear()

        for item in inp:
            if isinstance(item, dict):
                itype = item.get("type")

                # Responses API function_call -> assistant tool_calls
                if itype == "function_call":
                    call_id = item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                    name = item.get("name") or ""
                    arguments = item.get("arguments")
                    if not isinstance(arguments, str):
                        arguments = json.dumps(arguments or {})
                    pending_tool_calls.append({
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": arguments},
                    })
                    continue

                # Flush any buffered tool calls before processing a non-function_call item
                _flush_pending_tool_calls()

                # Responses API function_call_output -> tool role message
                if itype == "function_call_output":
                    call_id = item.get("call_id") or ""
                    output_val = item.get("output") or ""
                    if not isinstance(output_val, str):
                        output_val = json.dumps(output_val)
                    out.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": output_val,
                    })
                    continue

                if itype in ("input_text", "text", "output_text"):
                    txt = item.get("text")
                    if isinstance(txt, str) and txt.strip():
                        out.append({"role": "user", "content": txt.strip()})
                    continue
                role = _normalize_message_role(item.get("role"), default="user")
                content = item.get("content")
                text = _coerce_responses_content_to_text(content)
                if text:
                    out.append({"role": role, "content": text})
            elif isinstance(item, str):
                _flush_pending_tool_calls()
                s = item.strip()
                if s:
                    out.append({"role": "user", "content": s})
        _flush_pending_tool_calls()
        return out
    return out


def _inject_system_into_chat_messages(payload: Dict[str, Any]) -> None:
    """Prepend a system message if provided outside messages."""
    if not isinstance(payload, dict):
        return
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return
    # If a system message already exists, leave as-is.
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            return
    sys_texts = [
        payload.get("instructions"),
        payload.get("system"),
        payload.get("system_prompt"),
    ]
    for sys_text in sys_texts:
        if isinstance(sys_text, str) and sys_text.strip():
            messages.insert(0, {"role": "system", "content": sys_text.strip()})
            return


def _hoist_late_system_messages(payload: Dict[str, Any]) -> None:
    """Move system messages that appear after non-system roles to the front."""
    if not isinstance(payload, dict):
        return
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return

    first_non_system_idx = None
    for idx, msg in enumerate(messages):
        if isinstance(msg, dict) and msg.get("role") != "system":
            first_non_system_idx = idx
            break
    if first_non_system_idx is None:
        return

    late_system_texts: List[str] = []
    kept: List[Dict[str, Any]] = []
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        if idx >= first_non_system_idx and msg.get("role") == "system":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                late_system_texts.append(content.strip())
            continue
        kept.append(msg)

    if not late_system_texts:
        return

    target = None
    for msg in kept:
        if msg.get("role") == "system":
            target = msg
            break

    merged = "\n\n".join(late_system_texts)
    if target is None:
        kept.insert(0, {"role": "system", "content": merged})
    else:
        current = target.get("content")
        if isinstance(current, str) and current.strip():
            target["content"] = f"{current.strip()}\n\n{merged}"
        else:
            target["content"] = merged
    payload["messages"] = kept


def _normalize_message_role(role: Any, default: str = "user") -> str:
    if not isinstance(role, str):
        return default
    r = role.strip().lower()
    if r == "developer":
        return "system"
    if r in ("system", "user", "assistant", "tool"):
        return r
    return default


def _normalize_chat_messages_roles(payload: Dict[str, Any]) -> None:
    """Normalize chat roles for broader compatibility (e.g., developer -> system)."""
    if not isinstance(payload, dict):
        return
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if "role" in msg:
            msg["role"] = _normalize_message_role(msg.get("role"), default="user")


def _normalize_responses_roles(payload: Dict[str, Any]) -> None:
    """Normalize Responses API role-bearing items (e.g., developer -> system)."""
    if not isinstance(payload, dict):
        return
    for field in ("input", "messages"):
        items = payload.get(field)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            if "role" in item:
                item["role"] = _normalize_message_role(item.get("role"), default="user")


def _tool_call_id_aliases(value: Any) -> List[str]:
    """Build likely equivalent tool-call IDs (for truncated/prefixed variants)."""
    if not isinstance(value, str):
        return []
    raw = value.strip()
    if not raw:
        return []
    aliases: List[str] = [raw]
    queue: List[str] = [raw]

    def _add(candidate: Any) -> None:
        if not isinstance(candidate, str):
            return
        c = candidate.strip()
        if c and c not in aliases:
            aliases.append(c)
            queue.append(c)

    while queue:
        current = queue.pop(0)
        if "|" in current:
            for part in current.split("|"):
                _add(part)
        for prefix in ("chatcmpl-tool-", "call_", "call-"):
            if current.startswith(prefix) and len(current) > len(prefix):
                _add(current[len(prefix):])
        if "-" in current:
            tail = current.rsplit("-", 1)[-1]
            if len(tail) >= 8:
                _add(tail)
        if "_" in current:
            tail = current.rsplit("_", 1)[-1]
            if len(tail) >= 8:
                _add(tail)
    return aliases


_STRICT_TOOL_CALL_ID_RE = re.compile(r"^[A-Za-z0-9]{9}$")


def _to_strict_tool_call_id(
    value: Any,
    used_ids: set,
    alias_to_id: Dict[str, str],
) -> str:
    """Normalize arbitrary tool IDs to strict 9-char alnum IDs."""
    raw = value.strip() if isinstance(value, str) else ""
    aliases = _tool_call_id_aliases(raw) if raw else []
    if raw and raw not in aliases:
        aliases.insert(0, raw)

    for alias in aliases:
        mapped = alias_to_id.get(alias)
        if isinstance(mapped, str) and mapped:
            used_ids.add(mapped)
            return mapped

    candidates = aliases if aliases else [uuid.uuid4().hex]
    chosen: Optional[str] = None
    for cand in candidates:
        cleaned = re.sub(r"[^A-Za-z0-9]", "", cand)
        if not cleaned:
            continue
        if len(cleaned) >= 9:
            normalized = cleaned[-9:]
        else:
            normalized = (cleaned + uuid.uuid4().hex)[:9]
        if not _STRICT_TOOL_CALL_ID_RE.fullmatch(normalized):
            continue
        if normalized in used_ids:
            continue
        chosen = normalized
        break

    if not chosen:
        while True:
            normalized = uuid.uuid4().hex[:9]
            if normalized not in used_ids:
                chosen = normalized
                break

    used_ids.add(chosen)
    for alias in aliases:
        alias_to_id.setdefault(alias, chosen)
    alias_to_id.setdefault(chosen, chosen)
    return chosen


def _resolve_tool_call_id(
    tool_call_id: Any,
    seen_tool_call_ids: set,
    seen_tool_call_aliases: Dict[str, str],
) -> Optional[str]:
    aliases = _tool_call_id_aliases(tool_call_id)
    for alias in aliases:
        if alias in seen_tool_call_ids:
            return alias
        mapped = seen_tool_call_aliases.get(alias)
        if isinstance(mapped, str) and mapped:
            return mapped

    best_match: Optional[str] = None
    best_overlap = 0
    for alias in aliases:
        for seen in seen_tool_call_ids:
            overlap = min(len(alias), len(seen))
            if overlap < 8:
                continue
            if seen.endswith(alias) or alias.endswith(seen):
                if overlap > best_overlap:
                    best_match = seen
                    best_overlap = overlap
    if isinstance(best_match, str) and best_match:
        return best_match
    # In single-call flows (common in Cursor), preserve tool continuity even if
    # the provider omitted or heavily transformed the tool_call_id.
    if len(seen_tool_call_ids) == 1:
        only_seen = next(iter(seen_tool_call_ids))
        if isinstance(only_seen, str) and only_seen:
            return only_seen
    return None


def _normalize_chat_messages_content(payload: Dict[str, Any]) -> None:
    """Coerce chat message content/tool-call payloads to vLLM-compatible shapes."""
    if not isinstance(payload, dict):
        return
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return
    normalized_messages: List[Dict[str, Any]] = []
    seen_tool_call_ids: set = set()
    seen_tool_call_aliases: Dict[str, str] = {}
    strict_tool_call_ids: set = set()
    strict_tool_call_aliases: Dict[str, str] = {}
    for msg in messages:
        if not isinstance(msg, dict):
            continue

        prev_kept_role = (
            normalized_messages[-1].get("role")
            if normalized_messages and isinstance(normalized_messages[-1], dict)
            else None
        )

        role = msg.get("role")
        if role == "developer":
            role = "system"
        if role not in ("system", "user", "assistant", "tool"):
            role = "user"
        msg["role"] = role
        content = msg.get("content")
        if isinstance(content, list):
            msg["content"] = _coerce_responses_content_to_text(content)
        elif content is None and role in ("user", "assistant", "system", "developer", "tool"):
            msg["content"] = ""
        elif not isinstance(content, str):
            try:
                msg["content"] = json.dumps(content, ensure_ascii=False)
            except Exception:
                msg["content"] = str(content)

        # Normalize assistant tool calls to OpenAI chat shape.
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            clean_tool_calls: List[Dict[str, Any]] = []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function")
                if not isinstance(fn, dict):
                    continue
                name = fn.get("name")
                if not isinstance(name, str) or not name.strip():
                    # Drop malformed tool calls from prior history; they break strict upstream validation.
                    continue
                fn["name"] = name.strip()
                args = fn.get("arguments")
                if isinstance(args, (dict, list)):
                    fn["arguments"] = json.dumps(args, ensure_ascii=False)
                elif args is None:
                    fn["arguments"] = "{}"
                elif not isinstance(args, str):
                    fn["arguments"] = str(args)
                raw_tc_id = tc.get("id") if isinstance(tc.get("id"), str) and tc.get("id") else f"call_{uuid.uuid4().hex[:8]}"
                strict_tc_id = _to_strict_tool_call_id(raw_tc_id, strict_tool_call_ids, strict_tool_call_aliases)
                tc["id"] = strict_tc_id
                seen_tool_call_ids.add(strict_tc_id)
                for alias in _tool_call_id_aliases(raw_tc_id):
                    seen_tool_call_aliases.setdefault(alias, strict_tc_id)
                for alias in _tool_call_id_aliases(strict_tc_id):
                    seen_tool_call_aliases.setdefault(alias, strict_tc_id)
                tc["type"] = "function"
                clean_tool_calls.append(
                    {
                        "id": strict_tc_id,
                        "type": "function",
                        "function": {
                            "name": fn["name"],
                            "arguments": fn["arguments"],
                        },
                    }
                )
            if clean_tool_calls:
                msg["tool_calls"] = clean_tool_calls
            else:
                msg.pop("tool_calls", None)

        # Tool messages should always carry string content + tool_call_id.
        if role == "tool":
            if not isinstance(msg.get("content"), str):
                try:
                    msg["content"] = json.dumps(msg.get("content"), ensure_ascii=False)
                except Exception:
                    msg["content"] = str(msg.get("content") or "")
            # Convert common edit-tool mismatch into an actionable retry hint.
            # This helps the model break replace loops after a failed StrReplace attempt.
            tool_content_lc = (msg.get("content") or "").lower()
            if (
                "string to replace was not found" in tool_content_lc
                or "old_string" in tool_content_lc and "not found" in tool_content_lc
            ):
                msg["content"] = (
                    f"{msg.get('content')}\n"
                    "Hint: Re-read the file and retry with an exact old_string match from current file contents. "
                    "Use a short unique snippet, and keep new_string to code/content only."
                )
            if not msg.get("tool_call_id"):
                if isinstance(msg.get("call_id"), str) and msg.get("call_id"):
                    msg["tool_call_id"] = msg.get("call_id")
                elif isinstance(msg.get("id"), str) and msg.get("id"):
                    msg["tool_call_id"] = msg.get("id")
                else:
                    msg["tool_call_id"] = f"call_{uuid.uuid4().hex[:8]}"
            # Strict upstream rejects orphan/out-of-order tool role items.
            resolved_tool_call_id = _resolve_tool_call_id(
                msg.get("tool_call_id"), seen_tool_call_ids, seen_tool_call_aliases
            )
            if isinstance(resolved_tool_call_id, str) and resolved_tool_call_id:
                msg["tool_call_id"] = resolved_tool_call_id
            else:
                msg = {
                    "role": "user",
                    "content": (msg.get("content") or "").strip() or "Tool output received.",
                }
                role = "user"

        # Legacy single function_call payloads should also carry string arguments.
        function_call = msg.get("function_call")
        if isinstance(function_call, dict):
            name = function_call.get("name")
            if not isinstance(name, str) or not name.strip():
                msg.pop("function_call", None)
            else:
                function_call["name"] = name.strip()
            args = function_call.get("arguments")
            if isinstance(args, (dict, list)):
                function_call["arguments"] = json.dumps(args, ensure_ascii=False)
            elif args is None:
                function_call["arguments"] = "{}"
            elif not isinstance(args, str):
                function_call["arguments"] = str(args)

        # Drop non-spec message keys (Cursor includes id/type/metadata fields).
        if role == "assistant":
            has_tool_calls = isinstance(msg.get("tool_calls"), list) and len(msg.get("tool_calls")) > 0
            has_function_call = isinstance(msg.get("function_call"), dict)
            has_content = isinstance(msg.get("content"), str) and bool(msg.get("content").strip())
            # Empty assistant placeholders from prior tool streaming can trip strict validators.
            if not has_tool_calls and not has_function_call and not has_content:
                # But preserve/repair the assistant bridge after a tool block; strict
                # upstream can reject tool -> user transitions.
                if prev_kept_role == "tool":
                    msg["content"] = "Tool output received."
                else:
                    continue

        if role in ("system", "user"):
            allowed = {"role", "content", "name"}
        elif role == "assistant":
            allowed = {"role", "content", "name", "tool_calls", "function_call", "refusal"}
        else:  # tool
            allowed = {"role", "content", "tool_call_id"}
        normalized_messages.append({k: v for k, v in msg.items() if k in allowed})

    # Strict upstream can reject a direct tool -> user transition in long histories.
    repaired_messages: List[Dict[str, Any]] = []
    prev_role = None
    for msg in normalized_messages:
        role = msg.get("role") if isinstance(msg, dict) else None
        if role == "user" and prev_role == "tool":
            repaired_messages.append({"role": "assistant", "content": "Tool output received."})
            prev_role = "assistant"
        repaired_messages.append(msg)
        prev_role = role

    payload["messages"] = repaired_messages


def _normalize_chat_tools_shape(payload: Dict[str, Any]) -> None:
    """Convert flat function tools to nested OpenAI chat tools format."""
    if not isinstance(payload, dict):
        return
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return
    normalized: List[Dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        tool_type = tool.get("type") or "function"
        if tool_type != "function":
            continue

        fn = tool.get("function")
        if not isinstance(fn, dict):
            fn = {
                "name": tool.get("name"),
                "description": tool.get("description"),
                "parameters": (
                    tool.get("parameters")
                    or tool.get("input_schema")
                    or tool.get("json_schema")
                    or tool.get("schema")
                ),
            }

        name = fn.get("name")
        if isinstance(name, str) and name.startswith("functions/"):
            name = name.split("/", 1)[1]
        if not isinstance(name, str) or not name.strip():
            continue

        params = (
            fn.get("parameters")
            or fn.get("input_schema")
            or fn.get("json_schema")
            or fn.get("schema")
        )
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except Exception:
                params = None
        if isinstance(params, dict) and isinstance(params.get("schema"), dict):
            params = params.get("schema")
        if not isinstance(params, dict):
            params = {"type": "object", "properties": {}}

        desc = fn.get("description")
        normalized.append(
            {
                "type": "function",
                "function": {
                    "name": name.strip(),
                    "description": desc if isinstance(desc, str) else "",
                    "parameters": params,
                },
            }
        )

    if normalized:
        payload["tools"] = normalized


def _chat_sse_from_completion(resp_json: Dict[str, Any], include_usage: bool = False) -> List[bytes]:
    """Convert a non-stream chat completion into OpenAI-style SSE chunks."""
    out: List[bytes] = []
    try:
        choices = resp_json.get("choices") or []
        first = choices[0] if choices and isinstance(choices[0], dict) else {}
        msg = first.get("message") if isinstance(first.get("message"), dict) else {}
        finish_reason = first.get("finish_reason")
        cid = resp_json.get("id") or f"chatcmpl-{uuid.uuid4().hex[:16]}"
        created = resp_json.get("created") if isinstance(resp_json.get("created"), int) else int(time.time())
        model = resp_json.get("model") or DEFAULT_TEXT_MODEL

        delta: Dict[str, Any] = {"role": "assistant"}
        content = msg.get("content")
        if isinstance(content, str) and content:
            delta["content"] = content

        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            tc_out: List[Dict[str, Any]] = []
            for idx, tc in enumerate(tool_calls):
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                fn_name = fn.get("name")
                if not isinstance(fn_name, str) or not fn_name.strip():
                    continue
                fn_args = fn.get("arguments")
                if isinstance(fn_args, (dict, list)):
                    fn_args = json.dumps(fn_args, ensure_ascii=False)
                elif fn_args is None:
                    fn_args = "{}"
                elif not isinstance(fn_args, str):
                    fn_args = str(fn_args)
                tc_out.append(
                    {
                        "index": idx,
                        "id": tc.get("id") if isinstance(tc.get("id"), str) and tc.get("id") else f"call_{uuid.uuid4().hex[:8]}",
                        "type": "function",
                        "function": {"name": fn_name.strip(), "arguments": fn_args},
                    }
                )
            if tc_out:
                delta["tool_calls"] = tc_out

        out.append(
            _as_sse(
                {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                }
            )
        )

        final_finish = finish_reason
        if not isinstance(final_finish, str):
            final_finish = "tool_calls" if delta.get("tool_calls") else "stop"
        out.append(
            _as_sse(
                {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": final_finish}],
                }
            )
        )

        if include_usage and isinstance(resp_json.get("usage"), dict):
            out.append(
                _as_sse(
                    {
                        "id": cid,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [],
                        "usage": resp_json.get("usage"),
                    }
                )
            )
    except Exception:
        pass
    out.append(_as_sse("[DONE]"))
    return out


def _summarize_chat_payload_for_logs(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Log-safe payload summary for debugging upstream validation errors."""
    try:
        out: Dict[str, Any] = {}
        out["top_keys"] = sorted([str(k) for k in payload.keys()])[:40]
        messages = payload.get("messages")
        if isinstance(messages, list):
            out["message_count"] = len(messages)
            preview: List[Dict[str, Any]] = []
            for m in messages[:10]:
                if not isinstance(m, dict):
                    preview.append({"type": type(m).__name__})
                    continue
                role = m.get("role")
                content = m.get("content")
                item: Dict[str, Any] = {
                    "role": role,
                    "keys": sorted([str(k) for k in m.keys()])[:20],
                    "content_type": type(content).__name__,
                }
                if isinstance(content, str):
                    item["content_len"] = len(content)
                elif isinstance(content, list):
                    item["content_items"] = len(content)
                tool_calls = m.get("tool_calls")
                if isinstance(tool_calls, list):
                    item["tool_calls"] = len(tool_calls)
                    tc_preview: List[Dict[str, Any]] = []
                    for tc in tool_calls[:3]:
                        if not isinstance(tc, dict):
                            tc_preview.append({"type": type(tc).__name__})
                            continue
                        fn = tc.get("function")
                        args = fn.get("arguments") if isinstance(fn, dict) else None
                        tc_preview.append(
                            {
                                "keys": sorted([str(k) for k in tc.keys()])[:10],
                                "id_type": type(tc.get("id")).__name__,
                                "fn_keys": sorted([str(k) for k in fn.keys()])[:10] if isinstance(fn, dict) else [],
                                "args_type": type(args).__name__,
                                "args_len": len(args) if isinstance(args, str) else None,
                            }
                        )
                    item["tool_call_preview"] = tc_preview
                preview.append(item)
            out["messages_preview"] = preview
        tools = payload.get("tools")
        out["tools_count"] = len(tools) if isinstance(tools, list) else 0
        out["tool_choice_type"] = type(payload.get("tool_choice")).__name__
        out["stream"] = bool(payload.get("stream"))
        out["model"] = payload.get("model")
        return out
    except Exception:
        return {"error": "summary_failed"}


def _normalize_tool_names_in_payload(payload: Dict[str, Any]) -> None:
    """Strip 'functions/' prefix from tool names for compatibility."""
    if not isinstance(payload, dict):
        return
    tools = payload.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            # OpenAI tools format: {type:"function", function:{name:...}}
            func = tool.get("function")
            if isinstance(func, dict):
                name = func.get("name")
                if isinstance(name, str) and name.startswith("functions/"):
                    func["name"] = name.split("/", 1)[1]
            # Alternate: {name:...}
            name = tool.get("name")
            if isinstance(name, str) and name.startswith("functions/"):
                tool["name"] = name.split("/", 1)[1]
    tool_choice = payload.get("tool_choice")
    if isinstance(tool_choice, str) and tool_choice.startswith("functions/"):
        payload["tool_choice"] = tool_choice.split("/", 1)[1]
    elif isinstance(tool_choice, dict):
        fn = tool_choice.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
            if isinstance(name, str) and name.startswith("functions/"):
                fn["name"] = name.split("/", 1)[1]


def _ensure_tool_choice(payload: Dict[str, Any]) -> None:
    """If tools are present and tool_choice is unset, default to auto."""
    if not isinstance(payload, dict):
        return
    tools = payload.get("tools")
    if not isinstance(tools, list) or not tools:
        return
    if payload.get("tool_choice") is None:
        payload["tool_choice"] = "auto"


def _tool_names_from_chat_payload(payload: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    if not isinstance(payload, dict):
        return names
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return names
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
        else:
            name = tool.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip().lower())
    return names


def _stabilize_tool_generation(payload: Dict[str, Any]) -> None:
    """Set conservative defaults for tool-calling if client omitted them."""
    if not isinstance(payload, dict):
        return
    tools = payload.get("tools")
    if not isinstance(tools, list) or not tools:
        return
    # Keep deterministic-ish behavior for structured tool args.
    if not isinstance(payload.get("temperature"), (int, float)):
        payload["temperature"] = 0.15
    if not isinstance(payload.get("top_p"), (int, float)):
        payload["top_p"] = 0.9


def _append_tool_calling_hint(payload: Dict[str, Any]) -> None:
    """Append a minimal system hint about tool calls for better compatibility."""
    if not ENABLE_TOOL_HINT:
        return
    if not isinstance(payload, dict):
        return
    tools = payload.get("tools")
    if not isinstance(tools, list) or not tools:
        return
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return
    hint = (
        "You have access to tools. Use them when appropriate by calling tool_calls. "
        "Always provide all required parameters for tool calls."
    )
    tool_names = _tool_names_from_chat_payload(payload)
    edit_markers = (
        "strreplace",
        "str_replace",
        "replace",
        "edit",
        "write",
        "apply_patch",
    )
    if any(any(marker in name for marker in edit_markers) for name in tool_names):
        hint = (
            f"{hint} For file edits: never put planning/reasoning text into files. "
            "Write only valid code or exact file content. "
            "Before StrReplace-style edits, read/search the target file first and use an exact old_string match. "
            "If old_string is not found, re-read and retry with a smaller exact snippet."
        )
    # Avoid duplicating hint
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system" and isinstance(msg.get("content"), str):
            if hint in msg["content"]:
                return
    # Keep system prompts at the front to satisfy strict upstream role ordering.
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                msg["content"] = f"{content.strip()}\n\n{hint}"
            else:
                msg["content"] = hint
            return
    messages.insert(0, {"role": "system", "content": hint})



def _set_responses_input_from_messages(payload: Dict[str, Any], messages: List[Dict[str, str]]) -> None:
    payload["input"] = messages

def _extract_last_user_from_responses(payload: Dict[str, Any]) -> str:
    msgs = _responses_input_to_messages(payload)
    return _extract_last_user_from_chat_messages(msgs)


async def _stream_upstream(
    client: httpx.AsyncClient,
    method: str,
    url_path: str,
    headers: Dict[str, str],
    body: bytes,
):
    async with client.stream(method, url_path, headers=headers, content=body) as r:
        r.raise_for_status()
        async for chunk in r.aiter_bytes():
            yield chunk


# Configure extracted tooling pipeline module
configure_tooling_pipeline(DEFAULT_TEXT_MODEL, _responses_input_to_messages)

# -----------------------------------------------------------------------------
# Routes (modularized)
# -----------------------------------------------------------------------------
register_aux_routes(
    app,
    {
        "_metrics_inc": _metrics_inc,
        "_error_response": _error_response,
        "_invokeai_generate": _invokeai_generate,
        "_sdxl_generate_comfy": _sdxl_generate_comfy,
        "_sdxl_generate_auto1111": _sdxl_generate_auto1111,
        "SD_BACKEND": SD_BACKEND,
        "SD_URL": SD_URL,
        "SD_TIMEOUT": SD_TIMEOUT,
        "_estimate_tokens": _estimate_tokens,
        "_update_usage_metrics": _update_usage_metrics,
        "_attach_usage_headers": _attach_usage_headers,
        "ALLOWED_IMAGE_MODELS": ALLOWED_IMAGE_MODELS,
        "ALLOWED_CHAT_MODELS": ALLOWED_CHAT_MODELS,
        "ALLOWED_EMBED_MODELS": ALLOWED_EMBED_MODELS,
        "CHAT_UPSTREAM": CHAT_UPSTREAM,
        "extract_token_from_auth_header": extract_token_from_auth_header,
        "_metrics_snapshot": _metrics_snapshot,
        "_record_feedback": _record_feedback,
    },
)

# -----------------------------------------------------------------------------
# Proxy with memory interception for /v1/chat/completions AND /v1/responses
# -----------------------------------------------------------------------------
@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def proxy(path: str, request: Request):
    auth = request.headers.get("authorization", "")
    email_hdr = request.headers.get("x-user-email")
    token = extract_token_from_auth_header(auth)

    user_row = None
    if token and token.startswith("pg_"):
        user_row = await get_user_record_by_pg_key(token)
    elif email_hdr:
        user_row = await get_user_record_by_email(email_hdr)

    if not user_row:
        return JSONResponse(status_code=401, content={"error": "missing/invalid credentials"})

    body_bytes = await request.body()
    original_body_bytes = body_bytes

    method = request.method.upper()
    p = path.strip("/").lower()

    is_chat_completions = (method == "POST" and p == "chat/completions")
    is_responses = (method == "POST" and p == "responses")
    is_embeddings = (method == "POST" and p == "embeddings")

    # Detect OpenWebUI
    is_openwebui = _is_openwebui(request)

    # Parse JSON payload when applicable
    payload: Dict[str, Any] = {}
    is_json_body = body_bytes and (request.headers.get("content-type", "").lower().startswith("application/json"))
    if method in ("POST", "PUT", "PATCH") and is_json_body:
        try:
            payload = json.loads(body_bytes or b"{}") if body_bytes else {}
        except Exception:
            payload = {}

    # Cursor and some OpenAI-compatible clients may send Responses-style payloads
    # to /v1/chat/completions when a custom base URL is configured.
    if is_chat_completions and isinstance(payload, dict) and payload:
        msgs = payload.get("messages")
        if (not isinstance(msgs, list) or not msgs) and payload.get("input") is not None:
            converted = _responses_input_to_messages(payload)
            if isinstance(converted, list) and converted:
                payload["messages"] = converted

        if not isinstance(payload.get("max_tokens"), int):
            mot = payload.get("max_output_tokens")
            if isinstance(mot, int):
                payload["max_tokens"] = mot

    # Ensure model + local tool routing
    if (is_chat_completions or is_responses) and isinstance(payload, dict) and payload:
        _canonicalize_model_name(payload)
        _ensure_model(payload)
        _enforce_model_allowlist(payload, ALLOWED_CHAT_MODELS, DEFAULT_TEXT_MODEL)
        if is_responses:
            _normalize_responses_roles(payload)
        _normalize_tool_names_in_payload(payload)
        _ensure_tool_choice(payload)
        _stabilize_tool_generation(payload)
        if is_chat_completions:
            _normalize_chat_messages_roles(payload)
            _normalize_chat_messages_content(payload)
            _normalize_chat_tools_shape(payload)
            _append_tool_calling_hint(payload)
            _inject_system_into_chat_messages(payload)
            _hoist_late_system_messages(payload)
    if is_embeddings and isinstance(payload, dict) and payload:
        _enforce_model_allowlist(payload, ALLOWED_EMBED_MODELS, DEFAULT_EMBED_MODEL)

    requested_stream = False
    if (is_chat_completions or is_responses) and isinstance(payload, dict):
        requested_stream = bool(payload.get("stream"))

    if is_openwebui and (is_chat_completions or is_responses) and isinstance(payload, dict):
        last_user = ""
        if is_chat_completions:
            msgs = payload.get("messages") or []
            if isinstance(msgs, list):
                last_user = _extract_last_user_from_chat_messages(msgs)
        else:
            last_user = _extract_last_user_from_responses(payload)
        meta = _detect_owui_background_task(last_user)
        if meta is not None:
            text = json.dumps(meta, ensure_ascii=False)
            model = payload.get("model") or DEFAULT_TEXT_MODEL
            if is_responses:
                resp = _make_responses_from_text(text, model)
                headers = _attach_usage_headers({}, resp.get("usage") or {})
                return JSONResponse(content=resp, headers=headers)
            if requested_stream:
                async def gen():
                    now = int(time.time())
                    usage = {
                        "prompt_tokens": _estimate_tokens(text),
                        "completion_tokens": 0,
                        "total_tokens": _estimate_tokens(text),
                    }
                    _update_usage_metrics(usage)
                    chunk = {
                        "id": f"chatcmpl-meta-{now}",
                        "object": "chat.completion.chunk",
                        "created": now,
                        "model": model,
                        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                    }
                    yield _as_sse(chunk)
                    yield _as_sse({"type": "usage", "usage": usage})
                    yield _as_sse("[DONE]")
                return StreamingResponse(gen(), status_code=200, media_type="text/event-stream")
            resp = _make_chat_completion_from_text(text, model)
            headers = _attach_usage_headers({}, resp.get("usage") or {})
            return JSONResponse(content=resp, headers=headers)

    # External API token caps to avoid excessively long generations.
    if (is_chat_completions or is_responses) and isinstance(payload, dict) and payload:
        try:
            is_external_api = not is_openwebui
            if is_external_api:
                if is_chat_completions:
                    max_tokens = payload.get("max_tokens")
                    if not isinstance(max_tokens, int) or max_tokens > EXTERNAL_CHAT_MAX_TOKENS:
                        payload["max_tokens"] = EXTERNAL_CHAT_MAX_TOKENS
                else:
                    # Keep both fields bounded for mixed client compatibility.
                    max_output_tokens = payload.get("max_output_tokens")
                    if not isinstance(max_output_tokens, int) or max_output_tokens > EXTERNAL_RESPONSES_MAX_TOKENS:
                        payload["max_output_tokens"] = EXTERNAL_RESPONSES_MAX_TOKENS
                    max_tokens = payload.get("max_tokens")
                    if not isinstance(max_tokens, int) or max_tokens > EXTERNAL_RESPONSES_MAX_TOKENS:
                        payload["max_tokens"] = EXTERNAL_RESPONSES_MAX_TOKENS
        except Exception:
            pass

    # Ensure minimum max_tokens for tool-bearing requests to prevent argument truncation
    if (is_chat_completions or is_responses) and isinstance(payload, dict) and payload:
        try:
            tools = payload.get("tools")
            if isinstance(tools, list) and tools:
                mt = payload.get("max_tokens")
                if isinstance(mt, int) and mt < TOOL_MIN_MAX_TOKENS:
                    payload["max_tokens"] = TOOL_MIN_MAX_TOKENS
                mot = payload.get("max_output_tokens")
                if isinstance(mot, int) and mot < TOOL_MIN_MAX_TOKENS:
                    payload["max_output_tokens"] = TOOL_MIN_MAX_TOKENS
        except Exception:
            pass

    # Memory injection (OpenWebUI only)
    if MEMORY_PROVIDER == "zep" and is_openwebui and (is_chat_completions or is_responses) and isinstance(payload, dict):
        session_id = openwebui_chat_session_id(request, user_row)

        if is_chat_completions:
            messages = payload.get("messages") or []
            last_user_msg = _extract_last_user_from_chat_messages(messages if isinstance(messages, list) else [])
        else:
            last_user_msg = _extract_last_user_from_responses(payload)

        injected = False
        if last_user_msg:
            async with httpx.AsyncClient() as zc:
                await zep_upsert_session(zc, session_id)
                mems = await zep_search_memory(zc, session_id, last_user_msg, MEMORY_TOP_K)

            # Inject only a small recent window (1 exchange) to avoid bloat
            if mems:
                prev_messages: List[Dict[str, str]] = []
                for mem in mems[-2:]:
                    if ": " in mem:
                        role_part, content = mem.split(": ", 1)
                        role = role_part.strip("[]") or "user"
                        if content.strip():
                            prev_messages.append({"role": role, "content": content.strip()})

                if prev_messages:
                    if is_chat_completions:
                        if isinstance(messages, list):
                            payload["messages"] = prev_messages + messages
                            injected = True
                    else:
                        existing = _responses_input_to_messages(payload)
                        payload["input"] = prev_messages + existing
                        injected = True

        if injected:
            body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    # If we changed JSON, re-serialize even if injected/model changed
    if (is_chat_completions or is_responses) and isinstance(payload, dict) and payload and is_json_body:
        if is_chat_completions:
            # Keep chat payload lean and avoid Responses-only fields that some
            # upstream chat endpoints may reject.
            for key in (
                "input",
                "max_output_tokens",
                "text",
                "reasoning",
                "store",
                "include",
                "metadata",
                "previous_response_id",
                "truncation",
            ):
                payload.pop(key, None)
        new_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if new_bytes != original_body_bytes:
            body_bytes = new_bytes

    # Fast-path: if user intent clearly targets a tool, emit tool call directly (stream-safe)
    # Disabled by default to allow natural tool inference - set ENABLE_SHORT_CIRCUIT=1 to enable
    if ENABLE_SHORT_CIRCUIT and (is_chat_completions or is_responses) and isinstance(payload, dict):
        short = _maybe_short_circuit_read(payload, is_chat_completions, is_responses)
        if not isinstance(short, dict):
            short = _maybe_short_circuit_web_fetch(payload, is_chat_completions, is_responses)
        if isinstance(short, dict):
            if is_responses and bool(payload.get("stream")):
                chunks = _responses_sse_from_response(short)
                return StreamingResponse(iter(chunks), media_type="text/event-stream")
            return JSONResponse(content=short, status_code=200)

    # Prepare upstream request
    fwd_headers = strip_auth(dict(request.headers))
    # Always let httpx compute Content-Length from the final body.
    fwd_headers.pop("content-length", None)

    # Timeouts: keep your Cloudflare-friendly posture
    timeout = httpx.Timeout(connect=30.0, read=90.0, write=30.0, pool=30.0)

    # Determine stream flag (chat/responses)
    stream = False
    responses_stream_fallback = False
    chat_stream_fallback = False
    chat_stream_include_usage = False
    if (is_chat_completions or is_responses) and isinstance(payload, dict):
        stream = bool(payload.get("stream"))

    # For tool-bearing chat requests, prefer non-stream upstream (stable arguments),
    # then synthesize SSE back to the client if stream was requested.
    if is_chat_completions and stream and isinstance(payload, dict):
        tools = payload.get("tools")
        if isinstance(tools, list) and tools:
            so = payload.get("stream_options")
            if isinstance(so, dict):
                chat_stream_include_usage = bool(so.get("include_usage"))
            chat_stream_fallback = True
            payload["stream"] = False
            stream = False

    original_stream = stream
    responses_via_chat = is_responses and RESPONSES_VIA_CHAT and not PREFER_NATIVE_RESPONSES
    if responses_via_chat and stream:
        # Prefer non-stream when emulating /v1/responses via chat completions
        responses_stream_fallback = True
        stream = False
        payload["stream"] = False
    if is_responses and original_stream and not responses_via_chat and RESPONSES_STREAM_FALLBACK:
        responses_stream_fallback = True
        stream = False
        payload["stream"] = False

    # Some clients send stream_options while stream=false; strict upstream rejects that.
    if (is_chat_completions or is_responses) and isinstance(payload, dict):
        if not bool(payload.get("stream")):
            payload.pop("stream_options", None)

    # Keep serialized request body in sync with stream-flag mutations above.
    if (is_chat_completions or is_responses) and isinstance(payload, dict) and is_json_body:
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    # If routing /v1/responses via /v1/chat/completions, convert payload
    if responses_via_chat and isinstance(payload, dict):
        original_payload = dict(payload)
        chat_messages = _responses_input_to_messages(payload)
        # Preserve tools from original responses payload
        original_tools = original_payload.get("tools")
        original_tool_choice = original_payload.get("tool_choice")
        payload = {
            "model": original_payload.get("model") or DEFAULT_TEXT_MODEL,
            "messages": chat_messages,
            "stream": False,
        }
        # Preserve commonly supported sampling controls.
        if isinstance(original_payload.get("temperature"), (int, float)):
            payload["temperature"] = original_payload.get("temperature")
        if isinstance(original_payload.get("top_p"), (int, float)):
            payload["top_p"] = original_payload.get("top_p")
        for key in ("presence_penalty", "frequency_penalty", "seed", "user", "stop"):
            if key in original_payload:
                payload[key] = original_payload.get(key)
        if isinstance(original_payload.get("max_output_tokens"), int):
            payload["max_tokens"] = original_payload.get("max_output_tokens")
        elif isinstance(original_payload.get("max_tokens"), int):
            payload["max_tokens"] = original_payload.get("max_tokens")
        # Forward tools to chat completions if present (convert format)
        if isinstance(original_tools, list) and original_tools:
            # Convert Responses API tool format to Chat Completions format
            chat_tools = []
            for tool in original_tools:
                if isinstance(tool, dict) and tool.get("type") == "function":
                    # If already has nested "function" key, use as-is
                    if "function" in tool:
                        chat_tools.append(tool)
                    else:
                        params = (
                            tool.get("parameters")
                            or tool.get("input_schema")
                            or tool.get("json_schema")
                            or tool.get("schema")
                        )
                        if isinstance(params, str):
                            try:
                                params = json.loads(params)
                            except Exception:
                                params = None
                        if isinstance(params, dict) and isinstance(params.get("schema"), dict):
                            params = params.get("schema")
                        if not isinstance(params, dict):
                            params = {"type": "object", "properties": {}}
                        # Convert flat format to nested format
                        chat_tools.append({
                            "type": "function",
                            "function": {
                                "name": tool.get("name"),
                                "description": tool.get("description", ""),
                                "parameters": params,
                            }
                        })
            if chat_tools:
                payload["tools"] = chat_tools
                if original_tool_choice is not None:
                    payload["tool_choice"] = original_tool_choice
                else:
                    payload["tool_choice"] = "auto"
        _enforce_model_allowlist(payload, ALLOWED_CHAT_MODELS, DEFAULT_TEXT_MODEL)
        _normalize_tool_names_in_payload(payload)
        _normalize_chat_messages_roles(payload)
        _normalize_chat_messages_content(payload)
        _normalize_chat_tools_shape(payload)
        _inject_system_into_chat_messages(payload)
        _append_tool_calling_hint(payload)
        _hoist_late_system_messages(payload)
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    upstream_base = UPSTREAM
    if is_chat_completions:
        upstream_base = CHAT_UPSTREAM
    elif is_responses:
        upstream_base = RESPONSES_UPSTREAM if not responses_via_chat else CHAT_UPSTREAM
    elif is_embeddings:
        upstream_base = EMBEDDINGS_UPSTREAM

    async with httpx.AsyncClient(base_url=upstream_base, timeout=timeout) as client:
        upstream_path = f"/v1/{path.lstrip('/')}"
        if responses_via_chat:
            upstream_path = "/v1/chat/completions"
        try:
            # STREAMING: normalized SSE (keeps upstream stream alive)
            if method == "POST" and stream:
                media_type = "text/event-stream"

                async def gen():
                    emitted_any = False
                    done = False
                    emitted_role = False
                    saw_content = False
                    saw_tool_call = False
                    last_id = None
                    last_model = None
                    stream_usage: Dict[str, int] = {}
                    silent_buffer = ""

                    try:
                        # IMPORTANT: open the upstream stream INSIDE the generator
                        async with httpx.AsyncClient(base_url=upstream_base, timeout=timeout) as sclient:
                            async with sclient.stream(
                                method, upstream_path, headers=fwd_headers, content=body_bytes
                            ) as r:
                                # If upstream errors, emit a single SSE error payload (so clients don't hang)
                                if r.status_code != 200:
                                    try:
                                        err_bytes = await r.aread()
                                        msg = (err_bytes.decode("utf-8", "ignore")[:500] or "upstream error")
                                    except Exception:
                                        msg = "upstream error"
                                    yield _as_sse({"error": {"message": msg, "status_code": r.status_code}})
                                    yield _as_sse("[DONE]")
                                    return

                                # Pass through proper SSE if present; otherwise wrap JSON-per-line as SSE.
                                async for raw_line in r.aiter_lines():
                                    if raw_line is None:
                                        continue
                                    line = raw_line.strip()
                                    if not line:
                                        continue

                                    # Upstream already SSE-framed (we must parse + strip reasoning before forwarding)
                                    if line.startswith("data:"):
                                        data = line[5:].strip()
                                        emitted_any = True

                                        # End of stream
                                        if data == "[DONE]":
                                            done = True
                                            break

                                        # Most upstream SSE lines are JSON
                                        try:
                                            obj = json.loads(data)

                                            # Track usage if provided
                                            try:
                                                usage = _extract_usage(obj)
                                                if usage:
                                                    stream_usage = usage
                                                _update_usage_metrics(usage)
                                            except Exception:
                                                pass

                                            # Track meta so fallback chunk looks normal
                                            if isinstance(obj, dict):
                                                last_id = obj.get("id") or last_id
                                                last_model = obj.get("model") or last_model

                                            # Buffer reasoning (but do NOT emit)
                                            try:
                                                choices = obj.get("choices") or []
                                                if choices and isinstance(choices[0], dict):
                                                    delta = choices[0].get("delta") or {}
                                                    if isinstance(delta, dict):
                                                        if delta.get("tool_calls") or delta.get("function_call"):
                                                            saw_tool_call = True
                                                        c_piece = delta.get("content")
                                                        if isinstance(c_piece, str) and c_piece:
                                                            saw_content = True
                                            except Exception:
                                                pass

                                            # Strip reasoning before forwarding
                                            obj = _strip_reasoning_from_chunk(obj)

                                            # For OpenAI chat streaming clients, drop non-choice events
                                            if is_chat_completions and isinstance(obj, dict) and "choices" not in obj and "error" not in obj:
                                                continue

                                            # Suppress heartbeat/silent tokens in streaming deltas (buffer prefixes)
                                            try:
                                                choices = obj.get("choices") or []
                                                if choices and isinstance(choices[0], dict):
                                                    delta = choices[0].get("delta") or {}
                                                    if isinstance(delta, dict) and "tool_calls" not in delta:
                                                        piece = delta.get("content")
                                                        if isinstance(piece, str) and piece:
                                                            candidate = silent_buffer + piece
                                                            if _is_silent_prefix(candidate):
                                                                silent_buffer = candidate
                                                                # If we matched the full token, keep suppressing.
                                                                if _is_silent_token(candidate):
                                                                    continue
                                                                # Suppress until we know it's not a silent token.
                                                                continue
                                                            if silent_buffer:
                                                                # The buffered prefix was not a silent token; emit it now.
                                                                delta["content"] = silent_buffer + piece
                                                                silent_buffer = ""
                                                                choices[0]["delta"] = delta
                                                                obj["choices"] = choices
                                                        if isinstance(piece, str) and piece:
                                                            saw_content = True
                                            except Exception:
                                                pass

                                            # Drop silent tokens (heartbeat / no_reply)
                                            try:
                                                choices = obj.get("choices") or []
                                                if choices and isinstance(choices[0], dict):
                                                    delta = choices[0].get("delta") or {}
                                                    if isinstance(delta, dict):
                                                        c_piece = delta.get("content")
                                                        if isinstance(c_piece, str) and _is_silent_token(c_piece):
                                                            continue
                                            except Exception:
                                                pass

                                            # Drop empty chunks (prevents lots of blank deltas)
                                            try:
                                                choices = obj.get("choices") or []
                                                if choices and isinstance(choices[0], dict):
                                                    delta = choices[0].get("delta") or {}
                                                    if isinstance(delta, dict):
                                                        if (delta.get("content") in ("", None)) and ("tool_calls" not in delta):
                                                            continue
                                            except Exception:
                                                pass

                                            # Some clients expect a role delta before content deltas.
                                            try:
                                                if is_chat_completions:
                                                    choices = obj.get("choices") or []
                                                    if choices and isinstance(choices[0], dict):
                                                        delta = choices[0].get("delta") or {}
                                                        if isinstance(delta, dict):
                                                            if isinstance(delta.get("role"), str) and delta.get("role"):
                                                                emitted_role = True
                                                            elif not emitted_role:
                                                                has_content = isinstance(delta.get("content"), str) and bool(delta.get("content"))
                                                                has_tool = bool(delta.get("tool_calls") or delta.get("function_call"))
                                                                has_finish = choices[0].get("finish_reason") is not None
                                                                if has_content or has_tool or has_finish:
                                                                    delta["role"] = "assistant"
                                                                    choices[0]["delta"] = delta
                                                                    obj["choices"] = choices
                                                                    emitted_role = True
                                            except Exception:
                                                pass

                                            yield _as_sse(obj)
                                        except Exception:
                                            # If it's not JSON, forward it verbatim
                                            yield (line + "\n\n").encode("utf-8")

                                        continue



                                    # JSON-per-line fallback
                                    try:
                                        obj = json.loads(line)
                                        usage = _extract_usage(obj)
                                        if usage:
                                            stream_usage = usage
                                        _update_usage_metrics(usage)
                                        # detect tool calls in non-SSE JSON-per-line
                                        if _has_tool_calls(obj):
                                            saw_tool_call = True
                                        obj = _strip_reasoning_from_chunk(obj)
                                        if is_chat_completions and isinstance(obj, dict) and "choices" not in obj and "error" not in obj:
                                            continue
                                    except Exception:
                                        continue

                                    try:
                                        if is_chat_completions and isinstance(obj, dict):
                                            choices = obj.get("choices") or []
                                            if choices and isinstance(choices[0], dict):
                                                delta = choices[0].get("delta") or {}
                                                if isinstance(delta, dict):
                                                    if isinstance(delta.get("role"), str) and delta.get("role"):
                                                        emitted_role = True
                                                    elif not emitted_role:
                                                        has_content = isinstance(delta.get("content"), str) and bool(delta.get("content"))
                                                        has_tool = bool(delta.get("tool_calls") or delta.get("function_call"))
                                                        has_finish = choices[0].get("finish_reason") is not None
                                                        if has_content or has_tool or has_finish:
                                                            delta["role"] = "assistant"
                                                            choices[0]["delta"] = delta
                                                            obj["choices"] = choices
                                                            emitted_role = True
                                    except Exception:
                                        pass

                                    emitted_any = True
                                    yield _as_sse(obj)

                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # stream died / client disconnected / upstream hiccup
                        _metrics_inc("stream_errors")
                        pass
                    finally:
                        # If model never emitted content, emit a safe fallback message
                        if not saw_content and not saw_tool_call:
                            delta: Dict[str, Any] = {"content": EMPTY_RESPONSE_TEXT}
                            if not emitted_role:
                                delta["role"] = "assistant"
                            fallback = {
                                "id": last_id or "chatcmpl-fallback",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": last_model or (payload.get("model") if isinstance(payload, dict) else DEFAULT_TEXT_MODEL),
                                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                            }
                            _metrics_inc("empty_response_fallbacks")
                            yield _as_sse(fallback)

                        if stream_usage and not is_chat_completions:
                            yield _as_sse({"type": "usage", "usage": stream_usage})

                        if not emitted_any and not is_chat_completions:
                            yield _as_sse({"warning": "no upstream stream data received"})
                        yield _as_sse("[DONE]")


                return StreamingResponse(gen(), status_code=200, media_type=media_type)



            # NON-STREAM: normal request
            upstream_resp = await client.request(
                request.method,
                upstream_path,
                headers=fwd_headers,
                content=body_bytes,
            )

            if is_chat_completions and upstream_resp.status_code >= 400:
                try:
                    err_excerpt = upstream_resp.text
                    if isinstance(err_excerpt, str):
                        err_excerpt = err_excerpt.replace("\n", " ").strip()
                    else:
                        err_excerpt = str(err_excerpt)
                    err_excerpt = err_excerpt[:2000]
                except Exception:
                    err_excerpt = "<unavailable>"
                try:
                    payload_summary = _summarize_chat_payload_for_logs(payload if isinstance(payload, dict) else {})
                except Exception:
                    payload_summary = {"error": "payload_summary_failed"}
                logger.warning(
                    "chat_upstream_validation_error status=%s upstream_path=%s summary=%s err=%s",
                    upstream_resp.status_code,
                    upstream_path,
                    json.dumps(payload_summary, ensure_ascii=False),
                    err_excerpt,
                )
        except httpx.HTTPError as e:
            _metrics_inc("upstream_errors")
            return _error_response(502, "upstream_http", "Upstream request failed.", f"Check upstream at {UPSTREAM} and retry.", str(e))
        except Exception as e:
            _metrics_inc("upstream_errors")
            return _error_response(500, "upstream_error", "Upstream proxy error.", "Retry or check gateway logs.", str(e))

        # After completion: write to Zep (OpenWebUI only, non-stream only)
        if MEMORY_PROVIDER == "zep" and is_openwebui and (is_chat_completions or is_responses):
            if upstream_resp.status_code == 200:
                try:
                    resp_json = upstream_resp.json()
                except Exception:
                    resp_json = {}

                try:
                    session_id = openwebui_chat_session_id(request, user_row)
                    # last user message (recompute from payload)
                    last_user = ""
                    if is_chat_completions:
                        msgs = (payload.get("messages") if isinstance(payload, dict) else []) or []
                        if isinstance(msgs, list):
                            last_user = _extract_last_user_from_chat_messages(msgs)
                        ai_text = ""
                        resp_json = _normalize_chat_response_schema(resp_json)
                        choices = resp_json.get("choices") or []
                        if choices and isinstance(choices[0], dict):
                            msg = choices[0].get("message") or {}
                            ai_text = (msg.get("content") or msg.get("reasoning") or "").strip()
                    else:
                        last_user = _extract_last_user_from_responses(payload if isinstance(payload, dict) else {})
                        ai_text = _extract_text_from_responses_output(resp_json)

                    msgs_to_write = []
                    if last_user:
                        msgs_to_write.append({"role": "user", "content": last_user})
                    if ai_text:
                        msgs_to_write.append({"role": "assistant", "content": ai_text})

                    if msgs_to_write:
                        async with httpx.AsyncClient() as zc:
                            await zep_upsert_session(zc, session_id)
                            await zep_write_messages(zc, session_id, msgs_to_write)
                except Exception:
                    pass

        # Relay upstream response (non-stream)
        safe_headers = {
            k: v
            for k, v in upstream_resp.headers.items()
            if k.lower() not in ("content-length", "transfer-encoding", "connection")
        }

        # Preserve upstream content-type (don't force JSON if upstream returns something else)
        ct = upstream_resp.headers.get("content-type") or "application/json; charset=utf-8"
        media_type = ct.split(";", 1)[0]

        if is_embeddings and ENABLE_EMBED_FALLBACK:
            try:
                emb_json = upstream_resp.json()
                if _should_use_local_embedding_fallback(emb_json):
                    _metrics_inc("embedding_fallbacks")
                    safe_headers["x-embeddings-fallback"] = "local-deterministic"
                    return JSONResponse(
                        content=_make_local_embedding_response(payload if isinstance(payload, dict) else {}),
                        status_code=200,
                        headers=safe_headers,
                    )
            except Exception:
                try:
                    pkeys = sorted((payload or {}).keys()) if isinstance(payload, dict) else []
                except Exception:
                    pkeys = []
                logger.exception(
                    "chat_response_normalization_failed status=%s media_type=%s payload_keys=%s",
                    upstream_resp.status_code,
                    media_type,
                    ",".join(pkeys[:20]),
                )

        # Normalize OpenAI chat completion for clients like OpenWebUI
        if is_chat_completions and upstream_resp.status_code == 200:
            try:
                # Some upstreams return SSE even when stream=false; parse to a single response.
                if media_type == "text/event-stream":
                    resp_json = _chat_completion_from_sse(upstream_resp.text, payload.get("model") if isinstance(payload, dict) else DEFAULT_TEXT_MODEL)
                else:
                    resp_json = upstream_resp.json()

                _log_tool_pipeline_stage("chat:raw", resp_json, True, False)
                resp_json = _normalize_chat_response_schema(resp_json)
                _log_tool_pipeline_stage("chat:after_normalize_schema", resp_json, True, False)
                resp_json = _recover_truncated_tool_calls(resp_json, payload if isinstance(payload, dict) else {}, True, False)
                _log_tool_pipeline_stage("chat:after_recover_truncated", resp_json, True, False)
                resp_json = _strip_reasoning_from_nonstream(resp_json)
                resp_json = _normalize_tool_call_names_in_resp(resp_json)
                resp_json = _patch_tool_call_arguments(resp_json, payload if isinstance(payload, dict) else {}, True, False)
                _log_tool_pipeline_stage("chat:after_patch_args", resp_json, True, False)
                resp_json = _maybe_force_web_fetch_tool_call(resp_json, payload if isinstance(payload, dict) else {}, True, False)
                resp_json = _maybe_promote_json_to_tool_call(resp_json, payload if isinstance(payload, dict) else {})
                resp_json = _validate_and_repair_tool_calls(resp_json, payload if isinstance(payload, dict) else {}, True, False)
                _log_tool_pipeline_stage("chat:after_validate", resp_json, True, False)
                resp_json = _finalize_read_tool_calls(resp_json, payload if isinstance(payload, dict) else {}, True, False)
                _log_tool_pipeline_stage("chat:after_finalize_read", resp_json, True, False)
                resp_json = _ensure_toolcall_finish_reason(resp_json)
                resp_json = _strip_tool_call_content(resp_json)
                resp_json = _strip_silent_reply(resp_json)
                resp_json = _ensure_nonempty_chat_content(resp_json)
                _log_tool_pipeline_stage("chat:final", resp_json, True, False)

                usage = _extract_usage(resp_json)
                _update_usage_metrics(usage)
                safe_headers = _attach_usage_headers(safe_headers, usage)
                # JSONResponse must advertise JSON even when upstream replied SSE.
                safe_headers.pop("content-type", None)

                if chat_stream_fallback:
                    async def gen():
                        for chunk in _chat_sse_from_completion(resp_json, include_usage=chat_stream_include_usage):
                            yield chunk
                    return StreamingResponse(gen(), status_code=200, media_type="text/event-stream")

                return JSONResponse(
                    content=resp_json,
                    status_code=upstream_resp.status_code,
                    headers=safe_headers,
                )
            except Exception:
                try:
                    pkeys = sorted((payload or {}).keys()) if isinstance(payload, dict) else []
                except Exception:
                    pkeys = []
                logger.exception(
                    "responses_response_normalization_failed status=%s media_type=%s via_chat=%s payload_keys=%s",
                    upstream_resp.status_code,
                    media_type,
                    responses_via_chat,
                    ",".join(pkeys[:20]),
                )

        if is_responses and upstream_resp.status_code == 200:
            try:
                resp_json = upstream_resp.json()
                _log_tool_pipeline_stage("responses:raw", resp_json, False, True)
                if responses_via_chat:
                    resp_json = _responses_from_chat_completion(resp_json)
                    _log_tool_pipeline_stage("responses:after_from_chat", resp_json, False, True)
                    resp_json = _normalize_tool_call_names_in_resp(resp_json)
                    resp_json = _recover_truncated_tool_calls(resp_json, payload if isinstance(payload, dict) else {}, False, True)
                    _log_tool_pipeline_stage("responses:after_recover_truncated", resp_json, False, True)
                    resp_json = _patch_tool_call_arguments(resp_json, payload if isinstance(payload, dict) else {}, False, True)
                    _log_tool_pipeline_stage("responses:after_patch_args", resp_json, False, True)
                    resp_json = _maybe_force_web_fetch_tool_call(resp_json, payload if isinstance(payload, dict) else {}, False, True)
                    resp_json = _patch_responses_tool_output(resp_json, payload if isinstance(payload, dict) else {})
                    resp_json = _maybe_promote_json_to_tool_call(resp_json, payload if isinstance(payload, dict) else {}, is_responses=True)
                    resp_json = _ensure_responses_output_text_and_usage(resp_json)
                else:
                    resp_json = _strip_reasoning_from_responses_output(resp_json)
                    resp_json = _normalize_tool_call_names_in_resp(resp_json)
                    resp_json = _recover_truncated_tool_calls(resp_json, payload if isinstance(payload, dict) else {}, False, True)
                    _log_tool_pipeline_stage("responses:after_recover_truncated", resp_json, False, True)
                    resp_json = _patch_tool_call_arguments(resp_json, payload if isinstance(payload, dict) else {}, False, True)
                    _log_tool_pipeline_stage("responses:after_patch_args", resp_json, False, True)
                    resp_json = _patch_responses_tool_output(resp_json, payload if isinstance(payload, dict) else {})
                    resp_json = _maybe_promote_json_to_tool_call(resp_json, payload if isinstance(payload, dict) else {}, is_responses=True)
                resp_json = _validate_and_repair_tool_calls(resp_json, payload if isinstance(payload, dict) else {}, False, True)
                _log_tool_pipeline_stage("responses:after_validate", resp_json, False, True)
                resp_json = _normalize_responses_output_text_blocks(resp_json)
                resp_json = _ensure_responses_output_text_and_usage(resp_json)
                resp_json = _normalize_responses_output_text_blocks(resp_json)
                resp_json = _maybe_force_web_fetch_tool_call(resp_json, payload if isinstance(payload, dict) else {}, False, True)
                resp_json = _finalize_read_tool_calls(resp_json, payload if isinstance(payload, dict) else {}, False, True)
                _log_tool_pipeline_stage("responses:after_finalize_read", resp_json, False, True)
                resp_json = _ensure_responses_output_text_and_usage(resp_json)
                resp_json = _normalize_responses_output_text_blocks(resp_json)
                _log_tool_pipeline_stage("responses:final", resp_json, False, True)

                usage = _extract_usage(resp_json)
                if responses_stream_fallback:
                    async def gen():
                        for chunk in _responses_sse_from_response(resp_json):
                            yield chunk
                    return StreamingResponse(gen(), status_code=200, media_type="text/event-stream")

                _update_usage_metrics(usage)
                safe_headers = _attach_usage_headers(safe_headers, usage)
                # JSONResponse must advertise JSON even when upstream replied SSE.
                safe_headers.pop("content-type", None)

                return JSONResponse(
                    content=resp_json,
                    status_code=upstream_resp.status_code,
                    headers=safe_headers,
                )
            except Exception:
                pass


        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=safe_headers,
            media_type=media_type,
        )
