import json
import logging
import os
import re
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

_DEFAULT_TEXT_MODEL = "gpt-oss:20b"
_RESPONSES_INPUT_TO_MESSAGES: Optional[Callable[[Dict[str, Any]], List[Dict[str, str]]]] = None
logger = logging.getLogger("ai-gateway.tooling")

_ARG_WRAPPER_KEYS = ("arguments", "args", "input", "payload", "parameters", "kwargs")
_PARTIAL_JSON_KEYS = ("partialJson", "partial_json", "partial", "partialArguments", "partial_arguments")

# OpenClaw workspace paths (for intelligent path resolution)
OPENCLAW_WORKSPACE = os.path.expanduser("~/.openclaw/workspace")
OPENCLAW_KNOWN_FILES = {
    "soul.md": "SOUL.md",
    "identity.md": "IDENTITY.md",
    "agents.md": "AGENTS.md",
    "user.md": "USER.md",
    "tools.md": "TOOLS.md",
    "heartbeat.md": "HEARTBEAT.md",
}

# OpenClaw startup files (read in order when tool call has no path)
OPENCLAW_STARTUP_FILES = [
    "SOUL.md",
    "USER.md",
    "IDENTITY.md",
]
HEARTBEAT_TOKEN = os.getenv("HEARTBEAT_TOKEN", "HEARTBEAT_OK")
SILENT_REPLY_TOKEN = os.getenv("SILENT_REPLY_TOKEN", "NO_REPLY")

def configure(default_text_model: str, responses_input_to_messages_fn: Optional[Callable[[Dict[str, Any]], List[Dict[str, str]]]] = None) -> None:
    global _DEFAULT_TEXT_MODEL, _RESPONSES_INPUT_TO_MESSAGES
    if isinstance(default_text_model, str) and default_text_model.strip():
        _DEFAULT_TEXT_MODEL = default_text_model.strip()
    _RESPONSES_INPUT_TO_MESSAGES = responses_input_to_messages_fn


def _responses_input_to_messages_proxy(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    if callable(_RESPONSES_INPUT_TO_MESSAGES):
        try:
            out = _RESPONSES_INPUT_TO_MESSAGES(payload)
            if isinstance(out, list):
                return out
        except Exception:
            pass
    out: List[Dict[str, str]] = []
    inp = payload.get("input") if isinstance(payload, dict) else None
    if inp is None and isinstance(payload, dict):
        inp = payload.get("messages")
    if isinstance(inp, str):
        s = inp.strip()
        if s:
            out.append({"role": "user", "content": s})
        return out
    if isinstance(inp, list):
        for item in inp:
            if isinstance(item, dict):
                role = item.get("role") or "user"
                if isinstance(role, str):
                    r = role.strip().lower()
                    role = "system" if r == "developer" else (r if r in ("system", "user", "assistant", "tool") else "user")
                else:
                    role = "user"
                content = item.get("content")
                if isinstance(content, str) and content.strip():
                    out.append({"role": role, "content": content.strip()})
                elif isinstance(content, list):
                    parts: List[str] = []
                    for part in content:
                        if isinstance(part, dict):
                            txt = part.get("text")
                            if isinstance(txt, str) and txt.strip():
                                parts.append(txt.strip())
                    if parts:
                        out.append({"role": role, "content": "\n".join(parts)})
            elif isinstance(item, str) and item.strip():
                out.append({"role": "user", "content": item.strip()})
    return out

def _has_tool_calls(resp_json: Dict[str, Any]) -> bool:
    try:
        if not isinstance(resp_json, dict):
            return False
        if resp_json.get("tool_calls") or resp_json.get("function_call"):
            return True
        choices = resp_json.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return False
        c0 = choices[0]
        if c0.get("tool_calls") or c0.get("function_call"):
            return True
        msg = c0.get("message") or {}
        if isinstance(msg, dict) and (msg.get("tool_calls") or msg.get("function_call")):
            return True
        delta = c0.get("delta") or {}
        if isinstance(delta, dict) and (delta.get("tool_calls") or delta.get("function_call")):
            return True
    except Exception:
        return False
    return False


def _strip_tool_call_content(resp_json: Dict[str, Any]) -> Dict[str, Any]:
    """If a tool call is present, clear assistant content to avoid leaking internal reasoning."""
    try:
        if _has_tool_calls(resp_json):
            choices = resp_json.get("choices") or []
            if choices and isinstance(choices[0], dict):
                msg = choices[0].get("message") or {}
                if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                    msg["content"] = ""
                    choices[0]["message"] = msg
                    resp_json["choices"] = choices
    except Exception:
        pass
    return resp_json


def _normalize_tool_call_names_in_resp(resp_json: Dict[str, Any]) -> Dict[str, Any]:
    """Strip 'functions/' prefix from tool/function call names in chat + responses payloads."""
    try:
        if not isinstance(resp_json, dict):
            return resp_json
        # Chat Completions path
        choices = resp_json.get("choices") or []
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            if isinstance(msg, dict):
                tool_calls = msg.get("tool_calls")
                if isinstance(tool_calls, list):
                    for tc in tool_calls:
                        if isinstance(tc, dict):
                            fn = tc.get("function")
                            if isinstance(fn, dict):
                                name = fn.get("name")
                                if isinstance(name, str) and name.startswith("functions/"):
                                    fn["name"] = name.split("/", 1)[1]
        # Responses API path
        output = resp_json.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                if item.get("type") != "function_call":
                    continue
                name = item.get("name")
                if isinstance(name, str) and name.startswith("functions/"):
                    item["name"] = name.split("/", 1)[1]
        return resp_json
    except Exception:
        return resp_json


def _ensure_toolcall_finish_reason(resp_json: Dict[str, Any]) -> Dict[str, Any]:
    """If tool calls exist, ensure finish_reason is tool_calls."""
    try:
        if not _has_tool_calls(resp_json):
            return resp_json
        choices = resp_json.get("choices") or []
        if choices and isinstance(choices[0], dict):
            fr = choices[0].get("finish_reason")
            if fr in (None, "", "stop"):
                choices[0]["finish_reason"] = "tool_calls"
                resp_json["choices"] = choices
    except Exception:
        pass
    return resp_json


def _is_silent_token(text: str) -> bool:
    if not isinstance(text, str):
        return False
    trimmed = text.strip()
    return trimmed in (HEARTBEAT_TOKEN, SILENT_REPLY_TOKEN)


def _is_silent_prefix(text: str) -> bool:
    if not isinstance(text, str):
        return False
    for token in (HEARTBEAT_TOKEN, SILENT_REPLY_TOKEN):
        if token.startswith(text):
            return True
    return False


def _strip_silent_reply(resp_json: Dict[str, Any]) -> Dict[str, Any]:
    """Remove heartbeat/silent tokens from assistant content."""
    try:
        choices = resp_json.get("choices") or []
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            if isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, str) and _is_silent_token(content):
                    msg["content"] = ""
                    choices[0]["message"] = msg
                    resp_json["choices"] = choices
    except Exception:
        pass
    return resp_json


def _recover_partial_tool_json(partial: str) -> Optional[Dict[str, Any]]:
    """Attempt to recover a valid dict from truncated JSON (e.g. from max_tokens cutoff).

    Strategy:
    1. Try closing open braces/brackets/quotes to make it parseable.
    2. Fall back to regex extraction of key-value pairs.
    3. Last resort: extract known argument names (url, path, query, text) from the string.
    """
    if not isinstance(partial, str) or not partial.strip():
        return None
    raw = partial.strip()

    # Strategy 1: close open JSON structures
    for attempt in range(3):
        candidate = raw
        # Close open strings
        quote_count = candidate.count('"') - candidate.count('\\"')
        if quote_count % 2 != 0:
            candidate += '"'
        # Close open brackets/braces
        open_braces = candidate.count('{') - candidate.count('}')
        open_brackets = candidate.count('[') - candidate.count(']')
        candidate += ']' * max(0, open_brackets)
        candidate += '}' * max(0, open_braces)
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        # Try trimming trailing incomplete value before closing
        # e.g. {"path": "SOUL.md", "content": "some trun
        trimmed = re.sub(r',\s*"[^"]*":\s*"[^"]*$', '', raw)
        if trimmed != raw:
            raw = trimmed
            continue
        trimmed = re.sub(r',\s*"[^"]*":\s*$', '', raw)
        if trimmed != raw:
            raw = trimmed
            continue
        break

    # Strategy 2: regex extraction of key-value pairs
    pairs: Dict[str, Any] = {}
    # Match "key": "value" patterns
    for m in re.finditer(r'"(\w+)"\s*:\s*"((?:[^"\\]|\\.)*)"', partial):
        pairs[m.group(1)] = m.group(2)
    # Match "key": number patterns
    for m in re.finditer(r'"(\w+)"\s*:\s*(-?\d+(?:\.\d+)?)', partial):
        key = m.group(1)
        if key not in pairs:
            try:
                pairs[key] = json.loads(m.group(2))
            except Exception:
                pass
    # Match "key": true/false/null
    for m in re.finditer(r'"(\w+)"\s*:\s*(true|false|null)', partial):
        key = m.group(1)
        if key not in pairs:
            pairs[key] = json.loads(m.group(2))
    if pairs:
        return pairs

    # Strategy 3: extract known argument names from the raw string
    known_extractors = {
        "url": lambda s: (re.search(r'(https?://[^\s\'"<>\\]+)', s) or (None,)).group(0) if re.search(r'(https?://[^\s\'"<>\\]+)', s) else None,
        "path": lambda s: _extract_path_from_text(s),
        "query": None,  # no generic extractor
        "text": None,
    }
    result: Dict[str, Any] = {}
    for key, extractor in known_extractors.items():
        if key in partial.lower() and extractor:
            val = extractor(partial)
            if val:
                result[key] = val
    return result if result else None


def _parse_partial_json_value(raw: Any) -> Optional[Dict[str, Any]]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                return obj
        except Exception:
            recovered = _recover_partial_tool_json(s)
            if isinstance(recovered, dict) and recovered:
                return recovered
    return None


def _extract_partial_json_obj(obj: Any) -> Dict[str, Any]:
    if not isinstance(obj, dict):
        return {}
    for key in _PARTIAL_JSON_KEYS:
        parsed = _parse_partial_json_value(obj.get(key))
        if isinstance(parsed, dict) and parsed:
            return parsed
    fn = obj.get("function")
    if isinstance(fn, dict):
        for key in _PARTIAL_JSON_KEYS:
            parsed = _parse_partial_json_value(fn.get(key))
            if isinstance(parsed, dict) and parsed:
                return parsed
    return {}


def _recover_truncated_tool_calls(
    resp_json: Dict[str, Any],
    payload: Dict[str, Any],
    is_chat: bool,
    is_responses: bool,
) -> Dict[str, Any]:
    """Recover tool calls truncated by max_tokens (finish_reason: 'length').

    When vLLM hits the token limit while generating tool call arguments, it returns
    finish_reason: 'length' with truncated/partial JSON. This function detects that
    condition and attempts to salvage the arguments.
    """
    try:
        if not isinstance(resp_json, dict) or not isinstance(payload, dict):
            return resp_json

        if is_chat:
            choices = resp_json.get("choices") or []
            if not choices or not isinstance(choices[0], dict):
                return resp_json
            c0 = choices[0]
            if c0.get("finish_reason") != "length":
                return resp_json

            msg = c0.get("message") or {}
            if not isinstance(msg, dict):
                return resp_json

            tool_calls = msg.get("tool_calls")
            if not isinstance(tool_calls, list) or not tool_calls:
                # No tool calls but finish_reason is length -- check if content has tool-like JSON
                content = msg.get("content") or ""
                if isinstance(content, str) and content.strip():
                    tools = _tool_defs_from_payload(payload)
                    if tools:
                        tool_name, args = _extract_tool_name_and_args(content.strip(), tools)
                        if tool_name and isinstance(args, dict):
                            call_id = f"call_{uuid.uuid4().hex[:8]}"
                            msg["tool_calls"] = [
                                {"id": call_id, "type": "function", "function": {"name": tool_name, "arguments": json.dumps(args)}}
                            ]
                            msg["content"] = ""
                            c0["message"] = msg
                            c0["finish_reason"] = "tool_calls"
                            choices[0] = c0
                            resp_json["choices"] = choices
                return resp_json

            # We have tool calls but they might have truncated arguments
            content = msg.get("content") or ""
            schemas = _tool_schema_map_from_payload(payload)
            recovered_any = False

            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function")
                if not isinstance(fn, dict):
                    continue

                args_raw = fn.get("arguments")
                args_obj = _coerce_tool_arguments_obj(args_raw)
                partial_obj = _extract_partial_json_obj(tc)
                if partial_obj:
                    for k, v in partial_obj.items():
                        if k not in args_obj or args_obj.get(k) in (None, "", {}, []):
                            args_obj[k] = v
                name = fn.get("name") or ""
                schema = schemas.get(name)
                required = schema.get("required", []) if isinstance(schema, dict) else []

                # Check if arguments are empty or missing required fields
                missing = [r for r in required if not args_obj.get(r)]
                if not missing and args_obj:
                    if partial_obj:
                        fn["arguments"] = json.dumps(args_obj, ensure_ascii=False)
                        tc["function"] = fn
                        recovered_any = True
                    continue  # arguments look complete

                # Try to recover from partialJson-style data in the raw arguments string
                partial_source = args_raw if isinstance(args_raw, str) else ""
                # Also try assistant content which often contains the partial tool call text
                if not partial_source and isinstance(content, str):
                    partial_source = content

                if partial_source:
                    recovered = _recover_partial_tool_json(partial_source)
                    if isinstance(recovered, dict) and recovered:
                        # Merge recovered into existing args (recovered wins for missing keys)
                        for k, v in recovered.items():
                            if k not in args_obj or (isinstance(args_obj.get(k), str) and not args_obj[k].strip()):
                                args_obj[k] = v
                        fn["arguments"] = json.dumps(args_obj, ensure_ascii=False)
                        tc["function"] = fn
                        recovered_any = True
                        logger.info(
                            "Recovered truncated chat tool args tool=%s keys=%s",
                            name,
                            ",".join(sorted(args_obj.keys())[:8]),
                        )

            if recovered_any:
                msg["tool_calls"] = tool_calls
                c0["message"] = msg
                c0["finish_reason"] = "tool_calls"
                choices[0] = c0
                resp_json["choices"] = choices

        if is_responses:
            # For Responses API, check if output has function_calls with empty args
            output = resp_json.get("output")
            if not isinstance(output, list):
                return resp_json

            # Check for truncation indicators
            status = resp_json.get("status") or ""
            # Responses API may use "incomplete" status for truncation
            has_truncation = status in ("incomplete", "truncated")
            if not has_truncation:
                # Also check if any function_call has empty arguments
                has_truncation = any(
                    isinstance(item, dict) and item.get("type") == "function_call"
                    and _coerce_tool_arguments_obj(item.get("arguments")) == {}
                    for item in output
                )
            if not has_truncation:
                return resp_json

            schemas = _tool_schema_map_from_payload(payload)
            output_text = resp_json.get("output_text") or ""

            for item in output:
                if not isinstance(item, dict) or item.get("type") != "function_call":
                    continue
                name = item.get("name") or ""
                args_obj = _coerce_tool_arguments_obj(item.get("arguments"))
                partial_obj = _extract_partial_json_obj(item)
                if partial_obj:
                    for k, v in partial_obj.items():
                        if k not in args_obj or args_obj.get(k) in (None, "", {}, []):
                            args_obj[k] = v
                schema = schemas.get(name)
                required = schema.get("required", []) if isinstance(schema, dict) else []
                missing = [r for r in required if not args_obj.get(r)]
                if not missing and args_obj:
                    if partial_obj:
                        item["arguments"] = json.dumps(args_obj, ensure_ascii=False)
                    continue

                partial_source = item.get("arguments") if isinstance(item.get("arguments"), str) else ""
                if not partial_source and isinstance(output_text, str):
                    partial_source = output_text

                if partial_source:
                    recovered = _recover_partial_tool_json(partial_source)
                    if isinstance(recovered, dict) and recovered:
                        for k, v in recovered.items():
                            if k not in args_obj or (isinstance(args_obj.get(k), str) and not args_obj[k].strip()):
                                args_obj[k] = v
                        item["arguments"] = json.dumps(args_obj, ensure_ascii=False)
                        logger.info(
                            "Recovered truncated responses tool args tool=%s keys=%s",
                            name,
                            ",".join(sorted(args_obj.keys())[:8]),
                        )

            resp_json["output"] = output

    except Exception:
        logger.exception("truncated_tool_call_recovery_failed")
    return resp_json


def _tool_defs_from_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    tools = payload.get("tools")
    return tools if isinstance(tools, list) else []


def _extract_tool_params_schema(tool: Dict[str, Any], fn: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Best-effort extraction of a JSON schema from mixed tool formats."""
    candidates: List[Any] = []
    if isinstance(fn, dict):
        candidates.extend(
            [
                fn.get("parameters"),
                fn.get("input_schema"),
                fn.get("json_schema"),
                fn.get("schema"),
            ]
        )
    if isinstance(tool, dict):
        candidates.extend(
            [
                tool.get("parameters"),
                tool.get("input_schema"),
                tool.get("json_schema"),
                tool.get("schema"),
            ]
        )
    for raw in candidates:
        obj = raw
        if isinstance(obj, str):
            s = obj.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception:
                continue
        # Some formats nest actual schema under a "schema" key.
        if isinstance(obj, dict) and isinstance(obj.get("schema"), dict):
            obj = obj.get("schema")
        if isinstance(obj, dict):
            return obj
    return {}


def _normalize_tool_name(name: Any) -> str:
    if not isinstance(name, str):
        return ""
    n = name.strip()
    if n.startswith("functions/"):
        n = n.split("/", 1)[1]
    return n


def _args_effectively_empty(args: Any) -> bool:
    obj = _coerce_tool_arguments_obj(args)
    if not obj:
        return True
    for value in obj.values():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, dict) and not value:
            continue
        if isinstance(value, list) and not value:
            continue
        return False
    return True


def _infer_tool_name_from_args(args: Dict[str, Any], tools: List[Dict[str, Any]]) -> Optional[str]:
    args_keys = set(args.keys())
    best_name = None
    best_score = 0
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else None
        name = None
        params = None
        if fn:
            name = fn.get("name")
            params = _extract_tool_params_schema(tool, fn)
        if not name:
            name = tool.get("name")
            params = _extract_tool_params_schema(tool, None)
        if not isinstance(name, str):
            continue
        props = set()
        required = set()
        if isinstance(params, dict):
            props = set((params.get("properties") or {}).keys())
            required = set(params.get("required") or [])
        if required and not required.issubset(args_keys):
            continue
        if props:
            score = len(args_keys & props) - len(args_keys - props)
        else:
            score = 1
        if score > best_score:
            best_score = score
            best_name = name
    return best_name if best_score > 0 else None


def _extract_tool_name_and_args(content: str, tools: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    try:
        obj = json.loads(content)
    except Exception:
        return None, None
    if not isinstance(obj, dict):
        return None, None

    # Directly specified tool
    for key in ("tool", "name"):
        if isinstance(obj.get(key), str):
            tool_name = obj.get(key)
            args = obj.get("args") or obj.get("arguments") or {k: v for k, v in obj.items() if k not in ("tool", "name")}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    return tool_name, None
            return tool_name, args if isinstance(args, dict) else None

    # OpenAI-style wrapper
    fn = obj.get("function") if isinstance(obj.get("function"), dict) else None
    if fn and isinstance(fn.get("name"), str):
        tool_name = fn.get("name")
        args = fn.get("arguments") or fn.get("args") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                return tool_name, None
        return tool_name, args if isinstance(args, dict) else None

    # Infer from args-only JSON
    tool_name = _infer_tool_name_from_args(obj, tools)
    return tool_name, obj if tool_name else (None, None)


def _tool_names_from_payload(payload: Dict[str, Any]) -> List[str]:
    names = []
    for tool in _tool_defs_from_payload(payload):
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else None
        name = _normalize_tool_name(fn.get("name") if fn else tool.get("name"))
        if name:
            names.append(name)
    return names


def _extract_last_user_content_from_messages(messages: List[Dict[str, Any]]) -> Optional[str]:
    for msg in reversed(messages or []):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict):
                    txt = part.get("text") or part.get("output_text")
                    if isinstance(txt, str):
                        parts.append(txt)
            if parts:
                return "".join(parts)
    return None

def _is_user_turn_item(item: Any) -> bool:
    if isinstance(item, str):
        return bool(item.strip())
    if not isinstance(item, dict):
        return False
    role = item.get("role")
    if isinstance(role, str) and role.lower() == "user":
        return True
    itype = item.get("type")
    if isinstance(itype, str) and itype in ("input_text", "text"):
        return True
    return False


def _is_tool_output_turn_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    role = item.get("role")
    if isinstance(role, str) and role.lower() == "tool":
        return True
    itype = item.get("type")
    if isinstance(itype, str) and itype == "function_call_output":
        return True
    return False


def _has_tool_output_after_latest_user(payload: Dict[str, Any], is_chat: bool, is_responses: bool) -> bool:
    """
    Detect a post-tool round-trip state (tool output after the latest user turn).
    In that state, forcing/injecting another web tool call causes loops.
    """
    try:
        if not isinstance(payload, dict):
            return False
        items: Any = None
        if is_chat:
            items = payload.get("messages")
        elif is_responses:
            items = payload.get("input")
            if items is None:
                items = payload.get("messages")
        if not isinstance(items, list):
            return False

        last_user_idx = -1
        last_tool_output_idx = -1
        for idx, item in enumerate(items):
            if _is_user_turn_item(item):
                last_user_idx = idx
            if _is_tool_output_turn_item(item):
                last_tool_output_idx = idx
        if last_tool_output_idx < 0:
            return False
        if last_user_idx < 0:
            return True
        return last_tool_output_idx > last_user_idx
    except Exception:
        return False


def _collect_text_fragments_from_obj(obj: Any, out: List[str], depth: int = 0) -> None:
    if depth > 6 or len(out) >= 128:
        return
    if isinstance(obj, str):
        s = obj.strip()
        if s:
            out.append(s[:512])
        return
    if isinstance(obj, dict):
        for v in obj.values():
            _collect_text_fragments_from_obj(v, out, depth + 1)
        return
    if isinstance(obj, list):
        for it in obj:
            _collect_text_fragments_from_obj(it, out, depth + 1)


def _extract_text_from_payload_any(payload: Dict[str, Any]) -> str:
    try:
        frags: List[str] = []
        _collect_text_fragments_from_obj(payload, frags, 0)
        if not frags:
            return ""
        # Keep context bounded; enough for path/url inference.
        return "\n".join(frags)[:8192]
    except Exception:
        return ""


def _extract_best_effort_user_text(payload: Dict[str, Any], is_chat: bool, is_responses: bool) -> str:
    content = ""
    try:
        if is_chat:
            msgs = payload.get("messages")
            if isinstance(msgs, list):
                content = _extract_last_user_content_from_messages(msgs) or ""
        elif is_responses:
            msgs = _responses_input_to_messages_proxy(payload)
            content = _extract_last_user_content_from_messages(msgs) or ""
    except Exception:
        content = ""
    if content:
        return content
    return _extract_text_from_payload_any(payload)


def _extract_url_from_text(text: str) -> Optional[str]:
    if not isinstance(text, str):
        return None
    raw = text.strip()
    # Try to parse a JSON object embedded anywhere
    try:
        if raw.startswith("{") and raw.endswith("}"):
            obj = json.loads(raw)
            if isinstance(obj, dict) and isinstance(obj.get("url"), str):
                return obj.get("url")
    except Exception:
        pass
    # Try to extract JSON substring containing url
    try:
        m = re.search(r"\{[^}]*\burl\b[^}]*\}", raw)
        if m:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict) and isinstance(obj.get("url"), str):
                return obj.get("url")
    except Exception:
        pass
    # Fallback to URL regex
    m = re.search(r'(https?://|www\.)[^\s\)\]\}<>\\]+', raw)
    if m:
        return m.group(0)
    return None


def _tool_schema_map_from_payload(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    schemas: Dict[str, Dict[str, Any]] = {}
    for tool in _tool_defs_from_payload(payload):
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else None
        raw_name = fn.get("name") if fn else tool.get("name")
        name = _normalize_tool_name(raw_name)
        params = _extract_tool_params_schema(tool, fn)
        if not name:
            continue
        props = params.get("properties") if isinstance(params, dict) else {}
        required = params.get("required") if isinstance(params, dict) else []
        if not isinstance(props, dict):
            props = {}
        if not isinstance(required, list):
            required = []
        schema = {"properties": props, "required": [k for k in required if isinstance(k, str)]}
        schemas[name] = schema
        if isinstance(raw_name, str) and raw_name.strip() and raw_name != name:
            schemas[raw_name.strip()] = schema
    return schemas


def _resolve_openclaw_path(filename: str) -> Optional[str]:
    """Resolve OpenClaw workspace files to absolute paths."""
    if not isinstance(filename, str):
        return None

    # Check if it's already an absolute path
    if filename.startswith("/"):
        return filename if os.path.exists(filename) else None

    # Check if it's a known OpenClaw workspace file (case-insensitive)
    filename_lower = filename.lower()
    if filename_lower in OPENCLAW_KNOWN_FILES:
        full_path = os.path.join(OPENCLAW_WORKSPACE, OPENCLAW_KNOWN_FILES[filename_lower])
        return full_path if os.path.exists(full_path) else None

    # Try direct workspace path resolution (e.g., "SOUL.md" -> workspace/SOUL.md)
    if not "/" in filename:
        full_path = os.path.join(OPENCLAW_WORKSPACE, filename)
        if os.path.exists(full_path):
            return full_path

    return None


def _extract_path_from_text(text: str) -> Optional[str]:
    if not isinstance(text, str):
        return None
    raw = text.strip()

    # Prefer explicit code-style paths first.
    for pat in (
        r"`([^`\n]+)`",
        r"'([^'\n]+)'",
        r'"([^"\n]+)"',
    ):
        m = re.search(pat, raw)
        if m:
            candidate = (m.group(1) or "").strip()
            if candidate and ("/" in candidate or re.search(r"\.[A-Za-z0-9]{1,8}$", candidate)):
                resolved = _resolve_openclaw_path(candidate)
                if resolved:
                    return resolved
                return candidate.rstrip(".,;:!?")

    # Fallback: capture Unix-like absolute/relative paths (unquoted, no spaces).
    for pat in (
        r"(?:/|~/)[^\s`'\"<>]+(?:/[^\s`'\"<>]+)*",
        r"(?:[\w\-.]+/)+[\w\-.]+",
    ):
        m = re.search(pat, raw)
        if m:
            path = m.group(0).strip().rstrip(".,;:!?")
            resolved = _resolve_openclaw_path(path)
            if resolved:
                return resolved
            return path

    # Last resort: common bare filename forms (e.g., SOUL.md, soul.md).
    m = re.search(r"\b[\w\-.]+\.[A-Za-z0-9]{1,8}\b", raw)
    if m:
        filename = m.group(0).strip().rstrip(".,;:!?")
        # Try OpenClaw workspace resolution first
        resolved = _resolve_openclaw_path(filename)
        if resolved:
            return resolved
        return filename

    return None


def _coerce_tool_arguments_obj(args: Any) -> Dict[str, Any]:
    def _coerce(value: Any, depth: int = 0) -> Dict[str, Any]:
        if depth > 6:
            return {}

        if isinstance(value, dict):
            if not value:
                return {}

            # Prefer explicit nested argument wrappers emitted by some model/providers.
            for key in _ARG_WRAPPER_KEYS:
                if key not in value:
                    continue
                nested = _coerce(value.get(key), depth + 1)
                if nested:
                    return nested

            # Try side-channel partial JSON used by truncated tool calls.
            partial = _extract_partial_json_obj(value)
            if partial:
                base = dict(value)
                for key in _ARG_WRAPPER_KEYS:
                    base.pop(key, None)
                for key in _PARTIAL_JSON_KEYS:
                    base.pop(key, None)
                fn = base.get("function")
                if isinstance(fn, dict):
                    for key in _PARTIAL_JSON_KEYS:
                        fn.pop(key, None)
                    base["function"] = fn
                for k, v in partial.items():
                    if k not in base or base.get(k) in (None, "", {}, []):
                        base[k] = v
                return base if isinstance(base, dict) else partial

            return dict(value)

        if isinstance(value, str):
            s = value.strip()
            if not s:
                return {}
            try:
                parsed = json.loads(s)
            except Exception:
                recovered = _recover_partial_tool_json(s)
                return recovered if isinstance(recovered, dict) else {}
            return _coerce(parsed, depth + 1)

        if isinstance(value, list):
            for item in value:
                parsed = _coerce(item, depth + 1)
                if parsed:
                    return parsed
            return {}

        return {}

    out = _coerce(args)
    return out if isinstance(out, dict) else {}


def _infer_path_from_args_obj(args_obj: Dict[str, Any]) -> Optional[str]:
    """Best-effort path extraction from non-standard read arguments."""
    if not isinstance(args_obj, dict):
        return None
    for key, value in args_obj.items():
        k = str(key).lower()
        if any(tok in k for tok in ("path", "file", "filename", "target", "source")):
            if isinstance(value, str) and value.strip():
                inferred = _extract_path_from_text(value.strip())
                if inferred:
                    return inferred
                return value.strip().rstrip(".,;:!?")
    for value in args_obj.values():
        if isinstance(value, str) and value.strip():
            inferred = _extract_path_from_text(value.strip())
            if inferred:
                return inferred
    return None


def _infer_argument_value(arg_name: str, last_user: str, url: Optional[str]) -> Optional[str]:
    name = (arg_name or "").lower()
    if any(k in name for k in ("url", "uri", "link")):
        return url or None
    if any(k in name for k in ("query", "search", "keyword")):
        return url or (last_user.strip() if isinstance(last_user, str) and last_user.strip() else None)
    if any(k in name for k in ("path", "file", "filename")):
        return _extract_path_from_text(last_user)
    if any(k in name for k in ("text", "prompt", "input", "content", "message")):
        return last_user.strip() if isinstance(last_user, str) and last_user.strip() else None
    return None


def _repair_tool_arguments(
    tool_name: str,
    args: Any,
    schema: Optional[Dict[str, Any]],
    last_user: str,
    url: Optional[str],
) -> Dict[str, Any]:
    tool_name = _normalize_tool_name(tool_name)
    repaired = _coerce_tool_arguments_obj(args)
    required = schema.get("required", []) if isinstance(schema, dict) else []
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}

    # Tool-specific fast-path repair.
    if tool_name == "web_fetch" and not repaired.get("url") and url:
        repaired["url"] = url
    if tool_name == "web_search" and not repaired.get("query"):
        if url:
            repaired["query"] = url
        elif isinstance(last_user, str) and last_user.strip():
            repaired["query"] = last_user.strip()
    if tool_name == "read":
        path_val = repaired.get("path") or repaired.get("file_path") or repaired.get("filepath") or repaired.get("file")
        if not path_val:
            inferred_path = _infer_path_from_args_obj(repaired) or _extract_path_from_text(last_user)
            if inferred_path:
                if "path" in properties:
                    repaired["path"] = inferred_path
                elif "file_path" in properties:
                    repaired["file_path"] = inferred_path
                elif "filepath" in properties:
                    repaired["filepath"] = inferred_path
                elif "file" in properties:
                    repaired["file"] = inferred_path
                else:
                    repaired["path"] = inferred_path

    # Schema-based required arg repair.
    for req in required:
        current = repaired.get(req)
        if current is None or (isinstance(current, str) and not current.strip()):
            inferred = _infer_argument_value(req, last_user, url)
            if inferred is not None:
                repaired[req] = inferred

    return repaired


def _missing_required_arguments(args_obj: Dict[str, Any], schema: Optional[Dict[str, Any]]) -> List[str]:
    if not isinstance(args_obj, dict):
        return []
    required = schema.get("required", []) if isinstance(schema, dict) else []
    missing: List[str] = []
    for req in required:
        current = args_obj.get(req)
        if current is None or (isinstance(current, str) and not current.strip()):
            missing.append(req)
    return missing


def _validate_and_repair_tool_calls(
    resp_json: Dict[str, Any],
    payload: Dict[str, Any],
    is_chat: bool,
    is_responses: bool,
) -> Dict[str, Any]:
    try:
        if not isinstance(resp_json, dict) or not isinstance(payload, dict):
            return resp_json
        schemas = _tool_schema_map_from_payload(payload)

        content = _extract_best_effort_user_text(payload, is_chat, is_responses)
        url = _extract_url_from_text(content) if content else None
        unresolved_read = False
        unresolved_tools: List[str] = []

        if is_chat:
            choices = resp_json.get("choices") or []
            if choices and isinstance(choices[0], dict):
                msg = choices[0].get("message") or {}
                if isinstance(msg, dict):
                    tool_calls = msg.get("tool_calls")
                    if isinstance(tool_calls, list):
                        for tc in tool_calls:
                            if not isinstance(tc, dict):
                                continue
                            fn = tc.get("function")
                            if not isinstance(fn, dict):
                                continue
                            raw_name = fn.get("name")
                            name = _normalize_tool_name(raw_name)
                            if not name:
                                continue
                            schema = schemas.get(name) or schemas.get(raw_name)
                            repaired = _repair_tool_arguments(raw_name, fn.get("arguments"), schema, content, url)
                            missing_required = _missing_required_arguments(repaired, schema)
                            if missing_required:
                                unresolved_tools.append(f"{name}: {', '.join(missing_required)}")
                            if name == "read":
                                rv = repaired.get("path") or repaired.get("file_path") or repaired.get("filepath") or repaired.get("file")
                                if not (isinstance(rv, str) and rv.strip()):
                                    unresolved_read = True
                            fn["arguments"] = json.dumps(repaired, ensure_ascii=False)
                            tc["function"] = fn
                        msg["tool_calls"] = tool_calls
                        # Only block unresolved read calls; for other tools let the
                        # agent/tool runtime return structured errors and retry.
                        if unresolved_read:
                            details = "; ".join(unresolved_tools[:3]).strip()
                            prompt = "I need missing tool arguments before calling tools."
                            if details:
                                prompt = f"{prompt} Missing: {details}."
                            if unresolved_read:
                                prompt = f"{prompt} For file reads, include a path like `/path/to/file.md`."
                            msg.pop("tool_calls", None)
                            msg["content"] = prompt
                            choices[0]["finish_reason"] = "stop"
                        choices[0]["message"] = msg
                        resp_json["choices"] = choices

        if is_responses:
            out = resp_json.get("output")
            if isinstance(out, list):
                for item in out:
                    if not isinstance(item, dict) or item.get("type") != "function_call":
                        continue
                    raw_name = item.get("name")
                    name = _normalize_tool_name(raw_name)
                    if not name:
                        continue
                    schema = schemas.get(name) or schemas.get(raw_name)
                    repaired = _repair_tool_arguments(raw_name, item.get("arguments"), schema, content, url)
                    missing_required = _missing_required_arguments(repaired, schema)
                    if missing_required:
                        unresolved_tools.append(f"{name}: {', '.join(missing_required)}")
                    if name == "read":
                        rv = repaired.get("path") or repaired.get("file_path") or repaired.get("filepath") or repaired.get("file")
                        if not (isinstance(rv, str) and rv.strip()):
                            unresolved_read = True
                    item["arguments"] = json.dumps(repaired, ensure_ascii=False)
                resp_json["output"] = out
                if unresolved_read:
                    details = "; ".join(unresolved_tools[:3]).strip()
                    msg_text = "I need missing tool arguments before calling tools."
                    if details:
                        msg_text = f"{msg_text} Missing: {details}."
                    if unresolved_read:
                        msg_text = f"{msg_text} For file reads, include a path like `/path/to/file.md`."
                    msg_id = f"msg_{uuid.uuid4().hex[:8]}"
                    resp_json["status"] = "completed"
                    resp_json["output"] = [{
                        "type": "message",
                        "id": msg_id,
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": msg_text}],
                        "status": "completed",
                    }]
                    resp_json["output_text"] = msg_text
    except Exception:
        return resp_json
    return resp_json


def _finalize_read_tool_calls(
    resp_json: Dict[str, Any],
    payload: Dict[str, Any],
    is_chat: bool,
    is_responses: bool,
) -> Dict[str, Any]:
    """
    Last-ditch guardrail:
    - Never return a `read` tool call without a valid path.
    - If possible, repair from inferred user text.
    - Otherwise convert to assistant text asking for a path (breaks retry loops).
    """
    try:
        if not isinstance(resp_json, dict) or not isinstance(payload, dict):
            return resp_json
        inferred_path = _extract_path_from_text(_extract_best_effort_user_text(payload, is_chat, is_responses))

        if is_chat:
            choices = resp_json.get("choices") or []
            if choices and isinstance(choices[0], dict):
                c0 = choices[0]
                msg = c0.get("message") or {}
                if isinstance(msg, dict):
                    tcs = msg.get("tool_calls")
                    if isinstance(tcs, list) and tcs:
                        kept: List[Dict[str, Any]] = []
                        dropped_read = False
                        for tc in tcs:
                            if not isinstance(tc, dict):
                                continue
                            fn = tc.get("function")
                            if not isinstance(fn, dict):
                                kept.append(tc)
                                continue
                            name = _normalize_tool_name(fn.get("name"))
                            if name != "read":
                                kept.append(tc)
                                continue
                            args_obj = _coerce_tool_arguments_obj(fn.get("arguments"))
                            path_val = args_obj.get("path") or args_obj.get("file_path") or args_obj.get("filepath") or args_obj.get("file")
                            if isinstance(path_val, str) and path_val.strip():
                                kept.append(tc)
                                continue
                            if inferred_path:
                                fn["arguments"] = json.dumps({"path": inferred_path}, ensure_ascii=False)
                                tc["function"] = fn
                                kept.append(tc)
                            else:
                                dropped_read = True

                        if kept:
                            msg["tool_calls"] = kept
                            c0["message"] = msg
                            choices[0] = c0
                            resp_json["choices"] = choices
                        elif dropped_read:
                            msg.pop("tool_calls", None)
                            msg["content"] = "Please provide a file path to read (for example: `/path/to/file.md`)."
                            c0["message"] = msg
                            c0["finish_reason"] = "stop"
                            choices[0] = c0
                            resp_json["choices"] = choices
            return resp_json

        if is_responses:
            out = resp_json.get("output")
            if isinstance(out, list) and out:
                kept_items: List[Dict[str, Any]] = []
                dropped_read = False
                for item in out:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") != "function_call" or _normalize_tool_name(item.get("name")) != "read":
                        kept_items.append(item)
                        continue
                    args_obj = _coerce_tool_arguments_obj(item.get("arguments"))
                    path_val = args_obj.get("path") or args_obj.get("file_path") or args_obj.get("filepath") or args_obj.get("file")
                    if isinstance(path_val, str) and path_val.strip():
                        kept_items.append(item)
                        continue
                    if inferred_path:
                        item["arguments"] = json.dumps({"path": inferred_path}, ensure_ascii=False)
                        kept_items.append(item)
                    else:
                        dropped_read = True
                if kept_items:
                    resp_json["output"] = kept_items
                elif dropped_read:
                    text = "Please provide a file path to read (for example: `/path/to/file.md`)."
                    msg_id = f"msg_{uuid.uuid4().hex[:8]}"
                    resp_json["status"] = "completed"
                    resp_json["output"] = [{
                        "type": "message",
                        "id": msg_id,
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                        "status": "completed",
                    }]
                    resp_json["output_text"] = text
            return resp_json
    except Exception:
        return resp_json
    return resp_json


def _patch_tool_call_arguments(resp_json: Dict[str, Any], payload: Dict[str, Any], is_chat: bool, is_responses: bool) -> Dict[str, Any]:
    """Fill missing tool arguments from last user content."""
    try:
        if not isinstance(resp_json, dict) or not isinstance(payload, dict):
            return resp_json

        # Extract last user content for URL/path inference
        content = _extract_best_effort_user_text(payload, is_chat, is_responses)
        url = _extract_url_from_text(content or "") if content else None

        # Count empty read tool calls for OpenClaw startup file cycling
        empty_read_count = 0

        # Handle Chat Completions format
        if is_chat or not is_responses:
            choices = resp_json.get("choices") or []
            if not choices or not isinstance(choices[0], dict):
                return resp_json
            msg = choices[0].get("message") or {}
            if not isinstance(msg, dict):
                return resp_json
            tool_calls = msg.get("tool_calls")
            if not isinstance(tool_calls, list):
                return resp_json

            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function")
                if not isinstance(fn, dict):
                    continue
                name = _normalize_tool_name(fn.get("name"))
                args = fn.get("arguments")
                args_obj = _coerce_tool_arguments_obj(args)
                partial_obj = _extract_partial_json_obj(tc)
                partial_merged = False
                if partial_obj:
                    for k, v in partial_obj.items():
                        if k not in args_obj or args_obj.get(k) in (None, "", {}, []):
                            args_obj[k] = v
                            partial_merged = True
                empty = _args_effectively_empty(args)
                read_path_missing = (
                    name == "read"
                    and not (
                        isinstance(args_obj.get("path"), str) and args_obj.get("path").strip()
                        or isinstance(args_obj.get("file_path"), str) and args_obj.get("file_path").strip()
                        or isinstance(args_obj.get("filepath"), str) and args_obj.get("filepath").strip()
                        or isinstance(args_obj.get("file"), str) and args_obj.get("file").strip()
                    )
                )
                if partial_obj:
                    fn["arguments"] = json.dumps(args_obj, ensure_ascii=False)
                    tc["function"] = fn
                    if partial_merged:
                        logger.info(
                            "Merged chat partialJson into tool args tool=%s keys=%s",
                            name,
                            ",".join(sorted(args_obj.keys())[:8]),
                        )
                if not empty and not read_path_missing:
                    continue

                if name == "web_fetch" and url:
                    fn["arguments"] = json.dumps({"url": url})
                elif name == "web_search":
                    if url:
                        fn["arguments"] = json.dumps({"query": url})
                    elif isinstance(content, str) and content.strip():
                        fn["arguments"] = json.dumps({"query": content.strip()})
                elif name == "read":
                    inferred_path = _infer_path_from_args_obj(args_obj) or _extract_path_from_text(content or "")
                    if not inferred_path:
                        # OpenClaw fallback: cycle through startup files
                        if empty_read_count < len(OPENCLAW_STARTUP_FILES):
                            filename = OPENCLAW_STARTUP_FILES[empty_read_count]
                            inferred_path = os.path.join(OPENCLAW_WORKSPACE, filename)
                            empty_read_count += 1
                    if inferred_path:
                        fn["arguments"] = json.dumps({"path": inferred_path})
                # keep function name and update
                tc["function"] = fn

        # Handle Responses API format
        elif is_responses:
            output = resp_json.get("output")
            if not isinstance(output, list):
                return resp_json

            for item in output:
                if not isinstance(item, dict) or item.get("type") != "function_call":
                    continue
                name = _normalize_tool_name(item.get("name"))
                args = item.get("arguments")
                args_obj = _coerce_tool_arguments_obj(args)
                partial_obj = _extract_partial_json_obj(item)
                partial_merged = False
                if partial_obj:
                    for k, v in partial_obj.items():
                        if k not in args_obj or args_obj.get(k) in (None, "", {}, []):
                            args_obj[k] = v
                            partial_merged = True
                empty = _args_effectively_empty(args)
                read_path_missing = (
                    name == "read"
                    and not (
                        isinstance(args_obj.get("path"), str) and args_obj.get("path").strip()
                        or isinstance(args_obj.get("file_path"), str) and args_obj.get("file_path").strip()
                        or isinstance(args_obj.get("filepath"), str) and args_obj.get("filepath").strip()
                        or isinstance(args_obj.get("file"), str) and args_obj.get("file").strip()
                    )
                )
                if partial_obj:
                    item["arguments"] = json.dumps(args_obj, ensure_ascii=False)
                    if partial_merged:
                        logger.info(
                            "Merged responses partialJson into tool args tool=%s keys=%s",
                            name,
                            ",".join(sorted(args_obj.keys())[:8]),
                        )
                if not empty and not read_path_missing:
                    continue

                if name == "web_fetch" and url:
                    item["arguments"] = json.dumps({"url": url})
                elif name == "web_search":
                    if url:
                        item["arguments"] = json.dumps({"query": url})
                    elif isinstance(content, str) and content.strip():
                        item["arguments"] = json.dumps({"query": content.strip()})
                elif name == "read":
                    inferred_path = _infer_path_from_args_obj(args_obj) or _extract_path_from_text(content or "")
                    if not inferred_path:
                        # OpenClaw fallback: cycle through startup files
                        if empty_read_count < len(OPENCLAW_STARTUP_FILES):
                            filename = OPENCLAW_STARTUP_FILES[empty_read_count]
                            inferred_path = os.path.join(OPENCLAW_WORKSPACE, filename)
                            empty_read_count += 1
                    if inferred_path:
                        item["arguments"] = json.dumps({"path": inferred_path})

    except Exception:
        return resp_json
    return resp_json


def _maybe_force_web_fetch_tool_call(resp_json: Dict[str, Any], payload: Dict[str, Any], is_chat: bool, is_responses: bool) -> Dict[str, Any]:
    """If tools are available and user provided a URL, force a web_fetch tool call."""
    try:
        if not isinstance(resp_json, dict) or not isinstance(payload, dict):
            return resp_json
        if _has_tool_output_after_latest_user(payload, is_chat, is_responses):
            logger.debug("skip_force_web_fetch: post-tool round-trip detected")
            return resp_json
        if _has_tool_calls(resp_json):
            return resp_json
        tools = set(_tool_names_from_payload(payload))
        if "web_fetch" not in tools:
            return resp_json

        content = None
        if is_chat:
            msgs = payload.get("messages")
            if isinstance(msgs, list):
                content = _extract_last_user_content_from_messages(msgs)
        elif is_responses:
            msgs = _responses_input_to_messages_proxy(payload)
            content = _extract_last_user_content_from_messages(msgs)

        url = _extract_url_from_text(content or "") if content else None
        if not isinstance(url, str) or not url.strip():
            return resp_json

        call_id = f"call_{uuid.uuid4().hex[:8]}"
        if is_chat:
            choices = resp_json.get("choices") or []
            if not choices or not isinstance(choices[0], dict):
                return resp_json
            msg = choices[0].get("message") or {}
            if not isinstance(msg, dict):
                return resp_json
            msg["content"] = ""
            msg["tool_calls"] = [
                {"id": call_id, "type": "function", "function": {"name": "web_fetch", "arguments": json.dumps({"url": url})}}
            ]
            choices[0]["message"] = msg
            resp_json["choices"] = choices
            resp_json = _ensure_toolcall_finish_reason(resp_json)
        return resp_json
    except Exception:
        return resp_json


def _patch_responses_tool_output(resp_json: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """Fill missing tool arguments in Responses output or inject web_fetch when a URL is present."""
    try:
        if not isinstance(resp_json, dict) or not isinstance(payload, dict):
            return resp_json
        if _has_tool_output_after_latest_user(payload, False, True):
            logger.debug("skip_patch_responses_tool_output: post-tool round-trip detected")
            return resp_json

        tools = set(_tool_names_from_payload(payload))
        target_tool = "web_fetch" if "web_fetch" in tools else ("web_search" if "web_search" in tools else None)
        if target_tool is None:
            return resp_json

        # Extract URL from payload (best-effort)
        content = None
        msgs = _responses_input_to_messages_proxy(payload)
        content = _extract_last_user_content_from_messages(msgs)
        if not isinstance(content, str):
            try:
                content = json.dumps(payload, ensure_ascii=False)
            except Exception:
                content = None
        url = _extract_url_from_text(content or "") if content else None
        if not isinstance(url, str) or not url.strip():
            return resp_json

        output = resp_json.get("output")
        if not isinstance(output, list):
            output = []

        # Patch existing function_call items with empty args
        patched = False
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "function_call":
                continue
            args = item.get("arguments")
            empty = _args_effectively_empty(args)
            if empty:
                name = _normalize_tool_name(item.get("name"))
                if name == "web_fetch":
                    item["arguments"] = json.dumps({"url": url})
                    patched = True
                elif name == "web_search":
                    item["arguments"] = json.dumps({"query": url})
                    patched = True
                elif not name:
                    if target_tool == "web_fetch":
                        item["name"] = "web_fetch"
                        item["arguments"] = json.dumps({"url": url})
                    else:
                        item["name"] = "web_search"
                        item["arguments"] = json.dumps({"query": url})
                    patched = True

        # If no function_call item exists, inject one
        has_fc = any(isinstance(i, dict) and i.get("type") == "function_call" for i in output)
        if not has_fc:
            call_id = f"call_{uuid.uuid4().hex[:8]}"
            args_obj = {"url": url} if target_tool == "web_fetch" else {"query": url}
            output.append({
                "type": "function_call",
                "id": call_id,
                "call_id": call_id,
                "name": target_tool,
                "arguments": json.dumps(args_obj),
            })
            resp_json["status"] = "incomplete"
            resp_json["output"] = output
        elif patched:
            resp_json["output"] = output

        return resp_json
    except Exception:
        return resp_json



def _maybe_short_circuit_web_fetch(payload: Dict[str, Any], is_chat: bool, is_responses: bool) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    if _has_tool_output_after_latest_user(payload, is_chat, is_responses):
        return None
    tools = set(_tool_names_from_payload(payload))
    target_tool = "web_fetch" if "web_fetch" in tools else ("web_search" if "web_search" in tools else None)
    if target_tool is None:
        return None

    content = _extract_best_effort_user_text(payload, is_chat, is_responses)
    raw = content.strip() if isinstance(content, str) else ""
    url = _extract_url_from_text(raw) if raw else None
    if not isinstance(url, str) or not url.strip():
        return None

    call_id = f"call_{uuid.uuid4().hex[:8]}"
    args_obj = {"url": url} if target_tool == "web_fetch" else {"query": url}
    if is_chat:
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.get("model") or _DEFAULT_TEXT_MODEL,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {"name": target_tool, "arguments": json.dumps(args_obj)},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {},
        }
    if is_responses:
        return {
            "id": f"resp_{uuid.uuid4().hex[:8]}",
            "object": "response",
            "created_at": int(time.time()),
            "created": int(time.time()),
            "model": payload.get("model") or _DEFAULT_TEXT_MODEL,
            "status": "incomplete",
            "output": [
                {
                    "type": "function_call",
                    "id": call_id,
                    "call_id": call_id,
                    "name": target_tool,
                    "arguments": json.dumps(args_obj),
                }
            ],
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "output_text": "",
        }
    return None


def _maybe_short_circuit_read(payload: Dict[str, Any], is_chat: bool, is_responses: bool) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    tool_names = _tool_names_from_payload(payload)
    tools = set(tool_names)
    # If tools are declared, respect them. If tools are omitted, allow intent-based short-circuit.
    if tool_names and "read" not in tools:
        return None

    content = _extract_best_effort_user_text(payload, is_chat, is_responses)
    if not isinstance(content, str) or not content.strip():
        return None

    raw = content.strip()
    # Read intent + path-like target required.
    if not re.search(r"\b(read|open|show|display|view|cat|summari(?:ze|se))\b", raw, re.IGNORECASE):
        return None
    inferred_path = _extract_path_from_text(raw)
    if not inferred_path:
        return None
    if re.match(r"^https?://", inferred_path, re.IGNORECASE):
        return None

    call_id = f"call_{uuid.uuid4().hex[:8]}"
    args_obj = {"path": inferred_path}
    if is_chat:
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.get("model") or _DEFAULT_TEXT_MODEL,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {"name": "read", "arguments": json.dumps(args_obj)},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {},
        }
    if is_responses:
        return {
            "id": f"resp_{uuid.uuid4().hex[:8]}",
            "object": "response",
            "created_at": int(time.time()),
            "created": int(time.time()),
            "model": payload.get("model") or _DEFAULT_TEXT_MODEL,
            "status": "incomplete",
            "output": [
                {
                    "type": "function_call",
                    "id": call_id,
                    "call_id": call_id,
                    "name": "read",
                    "arguments": json.dumps(args_obj),
                }
            ],
            "usage": {},
        }
    return None


def _maybe_promote_json_to_tool_call(resp_json: Dict[str, Any], payload: Dict[str, Any], is_responses: bool = False) -> Dict[str, Any]:
    """Heuristic: if assistant content is JSON tool args, convert to tool_call.

    Works for both Chat Completions (choices[0].message.content) and
    Responses API (output_text or output message content).
    """
    try:
        if not isinstance(resp_json, dict) or not isinstance(payload, dict):
            return resp_json
        tools = _tool_defs_from_payload(payload)
        if not tools:
            return resp_json

        # --- Chat Completions path ---
        if not is_responses:
            if _has_tool_calls(resp_json):
                return resp_json
            choices = resp_json.get("choices") or []
            if not choices or not isinstance(choices[0], dict):
                return resp_json
            msg = choices[0].get("message") or {}
            if not isinstance(msg, dict):
                return resp_json
            content = msg.get("content")
            if not (isinstance(content, str) and content.strip().startswith("{")):
                return resp_json
            tool_name, args = _extract_tool_name_and_args(content, tools)
            if not isinstance(tool_name, str) or not isinstance(args, dict):
                return resp_json
            if tool_name.startswith("functions/"):
                tool_name = tool_name.split("/", 1)[1]
            call_id = f"call_{uuid.uuid4().hex[:8]}"
            msg["tool_calls"] = [
                {"id": call_id, "type": "function", "function": {"name": tool_name, "arguments": json.dumps(args)}}
            ]
            msg["content"] = ""
            choices[0]["message"] = msg
            resp_json["choices"] = choices
            resp_json = _ensure_toolcall_finish_reason(resp_json)
            return resp_json

        # --- Responses API path ---
        # Check if there are already function_call items in output
        output = resp_json.get("output")
        if isinstance(output, list):
            has_fc = any(isinstance(item, dict) and item.get("type") == "function_call" for item in output)
            if has_fc:
                return resp_json

        # Try output_text first
        output_text = resp_json.get("output_text") or ""
        content_to_check = ""
        if isinstance(output_text, str) and output_text.strip().startswith("{"):
            content_to_check = output_text.strip()
        elif isinstance(output, list):
            # Try extracting text from output message content
            for item in output:
                if not isinstance(item, dict):
                    continue
                item_content = item.get("content")
                if isinstance(item_content, list):
                    for part in item_content:
                        if isinstance(part, dict):
                            txt = part.get("text") or part.get("output_text") or ""
                            if isinstance(txt, str) and txt.strip().startswith("{"):
                                content_to_check = txt.strip()
                                break
                if content_to_check:
                    break

        if not content_to_check:
            return resp_json

        tool_name, args = _extract_tool_name_and_args(content_to_check, tools)
        if not isinstance(tool_name, str) or not isinstance(args, dict):
            return resp_json
        if tool_name.startswith("functions/"):
            tool_name = tool_name.split("/", 1)[1]

        call_id = f"call_{uuid.uuid4().hex[:8]}"
        resp_json["output"] = [{
            "type": "function_call",
            "id": call_id,
            "call_id": call_id,
            "name": tool_name,
            "arguments": json.dumps(args),
        }]
        resp_json["status"] = "incomplete"
        resp_json["output_text"] = ""
        return resp_json

    except Exception:
        return resp_json
