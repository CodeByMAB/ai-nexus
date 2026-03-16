# /opt/ai/gateway/response_normalization.py  — Response normalization, SSE helpers, reasoning stripping
import json
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Union

from metrics import _metrics_inc, _estimate_tokens, _update_usage_metrics, _extract_usage
from tooling_pipeline import (
    _has_tool_calls,
    _ensure_toolcall_finish_reason,
    _strip_silent_reply,
    _is_silent_token,
    _is_silent_prefix,
)

# -----------------------------------------------------------------------------
# Module-level config (set via configure())
# -----------------------------------------------------------------------------
_DEFAULT_TEXT_MODEL = "gpt-oss:20b"
_EMPTY_RESPONSE_TEXT = "I'm sorry, I couldn't generate a response. Please try again."


def configure(default_text_model: str, empty_response_text: str) -> None:
    global _DEFAULT_TEXT_MODEL, _EMPTY_RESPONSE_TEXT
    _DEFAULT_TEXT_MODEL = default_text_model
    _EMPTY_RESPONSE_TEXT = empty_response_text


# -----------------------------------------------------------------------------
# Responses API content helpers
# -----------------------------------------------------------------------------
def _coerce_responses_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, str):
                if part.strip():
                    parts.append(part.strip())
                continue
            if isinstance(part, dict):
                ptype = part.get("type")
                txt = None
                if ptype in ("input_text", "text", "output_text"):
                    txt = part.get("text")
                else:
                    txt = part.get("text")
                if isinstance(txt, str) and txt.strip():
                    parts.append(txt.strip())
        return "\n".join(parts).strip()
    return ""


def _maybe_extract_json_text(text: str) -> str:
    if not isinstance(text, str):
        return ""
    t = text.strip()
    if not t:
        return t

    # Strip markdown code fences.
    if t.startswith("```"):
        t = re.sub(r"^```[A-Za-z0-9_-]*\s*\n?", "", t)
        t = re.sub(r"\n?```$", "", t).strip()

    # Strip single XML-like wrapper tags often emitted by instruct models.
    m = re.match(r"^\s*<[^>]+>\s*(.*)\s*</[^>]+>\s*$", t, flags=re.DOTALL)
    if m:
        t = m.group(1).strip()

    # If JSON is embedded in extra prose, extract the outermost plausible block.
    for left, right in (("{", "}"), ("[", "]")):
        i = t.find(left)
        j = t.rfind(right)
        if i != -1 and j > i:
            candidate = t[i : j + 1].strip()
            try:
                json.loads(candidate)
                return candidate
            except Exception:
                pass

    return t


def _extract_text_from_responses_output(resp_json: Dict[str, Any]) -> str:
    ot = resp_json.get("output_text")
    if isinstance(ot, str) and ot.strip():
        return ot.strip()
    out = resp_json.get("output")
    if isinstance(out, list):
        chunks: List[str] = []
        for item in out:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    txt = part.get("text")
                    if isinstance(txt, str) and txt.strip():
                        chunks.append(txt)
        joined = "".join(chunks).strip()
        if joined:
            return joined
    return ""


def _ensure_responses_output_text_and_usage(resp_json: Dict[str, Any]) -> Dict[str, Any]:
    try:
        if "created_at" not in resp_json and isinstance(resp_json.get("created"), int):
            resp_json["created_at"] = resp_json.get("created")
        if "object" not in resp_json:
            resp_json["object"] = "response"

        text = _extract_text_from_responses_output(resp_json)
        if isinstance(text, str) and text.strip() and not resp_json.get("output_text"):
            resp_json["output_text"] = text.strip()
        usage = resp_json.get("usage") or {}
        if isinstance(usage, dict):
            if "input_tokens" not in usage and isinstance(usage.get("prompt_tokens"), int):
                usage["input_tokens"] = usage.get("prompt_tokens")
            if "output_tokens" not in usage and isinstance(usage.get("completion_tokens"), int):
                usage["output_tokens"] = usage.get("completion_tokens")
            if "total_tokens" not in usage and isinstance(usage.get("input_tokens"), int) and isinstance(usage.get("output_tokens"), int):
                usage["total_tokens"] = usage.get("input_tokens") + usage.get("output_tokens")
            resp_json["usage"] = usage
        else:
            resp_json["usage"] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

        if "status" not in resp_json:
            output = resp_json.get("output")
            if isinstance(output, list):
                function_calls = [
                    o for o in output
                    if isinstance(o, dict) and o.get("type") == "function_call"
                ]
                if function_calls:
                    has_in_progress = any(
                        isinstance(item.get("status"), str) and item.get("status") == "in_progress"
                        for item in function_calls
                    )
                    resp_json["status"] = "incomplete" if has_in_progress else "completed"
                else:
                    resp_json["status"] = "completed"
            else:
                resp_json["status"] = "completed"
    except Exception:
        pass
    return resp_json


def _responses_from_chat_completion(resp_json: Dict[str, Any]) -> Dict[str, Any]:
    try:
        choices = resp_json.get("choices") or []
        c0 = choices[0] if choices and isinstance(choices[0], dict) else {}
        msg = c0.get("message") if isinstance(c0, dict) else {}
        finish_reason = c0.get("finish_reason") if isinstance(c0, dict) else None
        text = ""
        tool_calls = []
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, str):
                text = content
                text = _maybe_extract_json_text(text)
            tc = msg.get("tool_calls")
            if isinstance(tc, list):
                tool_calls = tc
        usage = resp_json.get("usage") or {}
        if tool_calls:
            output_items = []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                name = fn.get("name")
                arguments = fn.get("arguments")
                partial_json = (
                    tc.get("partialJson")
                    or tc.get("partial_json")
                    or (fn.get("partialJson") if isinstance(fn, dict) else None)
                    or (fn.get("partial_json") if isinstance(fn, dict) else None)
                )
                call_id = tc.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                if isinstance(name, str):
                    parsed_args: Dict[str, Any]
                    if isinstance(arguments, str):
                        try:
                            loaded = json.loads(arguments)
                            parsed_args = loaded if isinstance(loaded, dict) else {}
                        except Exception:
                            parsed_args = {}
                    elif isinstance(arguments, dict):
                        parsed_args = dict(arguments)
                    else:
                        parsed_args = {}
                    normalized_arguments = arguments if isinstance(arguments, str) else json.dumps(arguments or {})
                    item = {
                        "type": "function_call",
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "call_id": call_id,
                        "name": name,
                        "arguments": normalized_arguments,
                        # Compatibility: some OpenAI-Responses clients prefer object-shaped args.
                        "input": parsed_args,
                        "status": "completed",
                    }
                    # Keep partial fragments only when we have no usable arguments yet.
                    if partial_json is not None and not item.get("arguments"):
                        item["partialJson"] = partial_json if isinstance(partial_json, str) else json.dumps(partial_json, ensure_ascii=False)
                    output_items.append(item)
            created_ts = resp_json.get("created") or int(time.time())
            status = "completed" if output_items else ("incomplete" if finish_reason == "length" else "completed")
            return {
                "id": resp_json.get("id") or "resp_fallback",
                "object": "response",
                "created": created_ts,
                "created_at": created_ts,
                "model": resp_json.get("model") or _DEFAULT_TEXT_MODEL,
                "status": status,
                "output": output_items,
                "usage": usage,
                "output_text": "",
            }
        created_ts = resp_json.get("created") or int(time.time())
        return {
            "id": resp_json.get("id") or "resp_fallback",
            "object": "response",
            "created": created_ts,
            "created_at": created_ts,
            "model": resp_json.get("model") or _DEFAULT_TEXT_MODEL,
            "output": [
                {
                    "type": "message",
                    "id": f"msg_{uuid.uuid4().hex[:8]}",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": text or _EMPTY_RESPONSE_TEXT,
                            "annotations": [],
                        }
                    ],
                }
            ],
            "usage": usage,
            "output_text": text or _EMPTY_RESPONSE_TEXT,
            "status": "completed",
        }
    except Exception:
        created_ts = int(time.time())
        return {
            "id": "resp_fallback",
            "object": "response",
            "created": created_ts,
            "created_at": created_ts,
            "model": _DEFAULT_TEXT_MODEL,
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": _EMPTY_RESPONSE_TEXT}],
                }
            ],
        }


# -----------------------------------------------------------------------------
# SSE helpers
# -----------------------------------------------------------------------------
def _as_sse(data_obj: Union[Dict[str, Any], str]) -> bytes:
    if isinstance(data_obj, str):
        return f"data: {data_obj}\n\n".encode("utf-8")
    return f"data: {json.dumps(data_obj, ensure_ascii=False)}\n\n".encode("utf-8")


def _responses_sse_from_response(resp_json: Dict[str, Any]) -> List[bytes]:
    out: List[bytes] = []
    try:
        resp_json = _ensure_responses_output_text_and_usage(resp_json)
        output = resp_json.get("output") if isinstance(resp_json, dict) else None
        if isinstance(output, list) and any(isinstance(o, dict) and o.get("type") == "function_call" for o in output):
            completed_items: List[Dict[str, Any]] = []
            for idx, item in enumerate(output):
                if not isinstance(item, dict):
                    continue
                if item.get("type") != "function_call":
                    continue
                added_item = dict(item)
                args_obj: Dict[str, Any] = {}
                raw_args_str = "{}"
                try:
                    raw_args = added_item.get("arguments")
                    if isinstance(raw_args, str):
                        raw_args_str = raw_args
                        parsed = json.loads(raw_args)
                        if isinstance(parsed, dict):
                            args_obj = parsed
                    elif isinstance(raw_args, dict):
                        raw_args_str = json.dumps(raw_args, ensure_ascii=False)
                        args_obj = dict(raw_args)
                except Exception:
                    args_obj = {}
                    raw_args_str = "{}"
                # Compatibility: mirror arguments in object form for clients that read `input`.
                if args_obj and not isinstance(added_item.get("input"), dict):
                    added_item["input"] = args_obj
                added_item.pop("partialJson", None)
                added_item.pop("partial_json", None)
                item_id = str(added_item.get("id") or f"fc_{uuid.uuid4().hex[:8]}")
                added_item["id"] = item_id
                if not isinstance(added_item.get("call_id"), str) or not added_item.get("call_id"):
                    added_item["call_id"] = f"call_{uuid.uuid4().hex[:8]}"
                added_item["status"] = "in_progress"
                # Keep full arguments on added for clients that execute on this event.
                added_item["arguments"] = raw_args_str
                done_item = dict(added_item)
                done_item["arguments"] = raw_args_str
                done_item["status"] = "completed"
                out.append(_as_sse({"type": "response.output_item.added", "output_index": idx, "item": added_item}))
                if raw_args_str:
                    out.append(_as_sse({
                        "type": "response.function_call_arguments.delta",
                        "item_id": item_id,
                        "output_index": idx,
                        "delta": raw_args_str,
                    }))
                out.append(_as_sse({
                    "type": "response.function_call_arguments.done",
                    "item_id": item_id,
                    "output_index": idx,
                    "arguments": raw_args_str,
                }))
                out.append(_as_sse({"type": "response.output_item.done", "output_index": idx, "item": done_item}))
                completed_items.append(done_item)
            resp = dict(resp_json)
            if completed_items:
                resp["output"] = completed_items
            if resp.get("status") in (None, "incomplete", "in_progress"):
                resp["status"] = "completed"
            out.append(_as_sse({"type": "response.completed", "response": resp}))
            out.append(_as_sse("[DONE]"))
            return out
        text = _extract_text_from_responses_output(resp_json)
        if not isinstance(text, str):
            text = ''
        text = text.strip()
        msg_id = resp_json.get("id") or f"msg_{uuid.uuid4().hex[:8]}"
        out.append(_as_sse({"type": "response.output_item.added", "output_index": 0, "item": {
            "type": "message",
            "id": msg_id,
            "role": "assistant",
            "content": [],
            "status": "in_progress"
        }}))
        out.append(_as_sse({"type": "response.content_part.added", "item_id": msg_id, "output_index": 0, "content_index": 0, "part": {
            "type": "output_text",
            "text": ""
        }}))
        if text:
            out.append(_as_sse({"type": "response.output_text.delta", "item_id": msg_id, "output_index": 0, "content_index": 0, "delta": text}))
            out.append(_as_sse({"type": "response.output_text.done", "item_id": msg_id, "output_index": 0, "content_index": 0, "text": text}))
        out.append(_as_sse({"type": "response.output_item.done", "output_index": 0, "item": {
            "type": "message",
            "id": msg_id,
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
            "status": "completed"
        }}))
        resp = dict(resp_json)
        resp.setdefault("status", "completed")
        out.append(_as_sse({"type": "response.completed", "response": resp}))
        out.append(_as_sse("[DONE]"))
    except Exception:
        out.append(_as_sse({"type": "response.completed", "response": {"status": "error"}}))
        out.append(_as_sse("[DONE]"))
    return out


# -----------------------------------------------------------------------------
# Chat completion normalization
# -----------------------------------------------------------------------------
def _ensure_nonempty_chat_content(resp_json: Dict[str, Any]) -> Dict[str, Any]:
    try:
        choices = resp_json.get("choices") or []
        if not choices:
            return resp_json
        c0 = choices[0] or {}
        msg = c0.get("message") or {}
        if isinstance(msg, dict):
            if _has_tool_calls(resp_json):
                return resp_json
            content = msg.get("content")
            if not (isinstance(content, str) and content.strip()):
                msg["content"] = _EMPTY_RESPONSE_TEXT
                c0["message"] = msg
                choices[0] = c0
                resp_json["choices"] = choices
                _metrics_inc("empty_response_fallbacks")
    except Exception:
        pass
    return resp_json


def _strip_reasoning_from_chunk(chunk: Dict[str, Any]) -> Dict[str, Any]:
    try:
        choices = chunk.get("choices") or []
        if choices and isinstance(choices[0], dict):
            delta = choices[0].get("delta") or {}
            if isinstance(delta, dict):
                tool_calls = delta.get("tool_calls")
                if isinstance(tool_calls, list):
                    for tc in tool_calls:
                        if isinstance(tc, dict):
                            fn = tc.get("function")
                            if isinstance(fn, dict):
                                name = fn.get("name")
                                if isinstance(name, str) and name.startswith("functions/"):
                                    fn["name"] = name.split("/", 1)[1]
                if delta.get("tool_calls") and isinstance(delta.get("content"), str):
                    delta.pop("content", None)
                if "content" not in delta and isinstance(delta.get("text"), str):
                    delta["content"] = delta.get("text")
                    delta.pop("text", None)
                removed = False
                for field in ("reasoning", "reasoning_content", "analysis"):
                    if field in delta:
                        delta.pop(field, None)
                        removed = True
                if removed:
                    _metrics_inc("reasoning_stripped")
                choices[0]["delta"] = delta
                chunk["choices"] = choices
    except Exception:
        pass
    return chunk


def _strip_reasoning_from_nonstream(resp_json: Dict[str, Any]) -> Dict[str, Any]:
    try:
        choices = resp_json.get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            removed = False
            for field in ("reasoning", "reasoning_content", "analysis"):
                if field in msg:
                    msg.pop(field, None)
                    removed = True
            if removed:
                _metrics_inc("reasoning_stripped")
            choices[0]["message"] = msg
            resp_json["choices"] = choices
        if "reasoning" in resp_json:
            resp_json.pop("reasoning", None)
    except Exception:
        pass
    return resp_json


def _normalize_chat_response_schema(resp_json: Dict[str, Any]) -> Dict[str, Any]:
    try:
        if not isinstance(resp_json, dict):
            return resp_json
        choices = resp_json.get("choices") or []
        if choices and isinstance(choices[0], dict):
            c0 = choices[0]
            if not isinstance(c0.get("message"), dict):
                txt = c0.get("text")
                if isinstance(txt, str):
                    c0["message"] = {"role": "assistant", "content": txt}
                    c0.pop("text", None)
                    choices[0] = c0
                    resp_json["choices"] = choices
        if not choices and isinstance(resp_json.get("output_text"), str):
            text = resp_json.get("output_text") or ""
            resp_json = {
                "id": resp_json.get("id") or "chatcmpl-fallback",
                "object": "chat.completion",
                "created": resp_json.get("created") or int(time.time()),
                "model": resp_json.get("model") or _DEFAULT_TEXT_MODEL,
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
                ],
                "usage": resp_json.get("usage") or {},
            }
    except Exception:
        pass
    return resp_json


def _make_chat_completion_from_text(text: str, model: str) -> Dict[str, Any]:
    now = int(time.time())
    usage = {
        "prompt_tokens": _estimate_tokens(text),
        "completion_tokens": 0,
        "total_tokens": _estimate_tokens(text),
    }
    _update_usage_metrics(usage)
    return {
        "id": f"chatcmpl-meta-{now}",
        "object": "chat.completion",
        "created": now,
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": usage,
        "output_text": text,
    }


def _make_responses_from_text(text: str, model: str) -> Dict[str, Any]:
    now = int(time.time())
    usage = {
        "prompt_tokens": _estimate_tokens(text),
        "completion_tokens": 0,
        "total_tokens": _estimate_tokens(text),
    }
    _update_usage_metrics(usage)
    return {
        "id": f"resp-meta-{now}",
        "object": "response",
        "created": now,
        "model": model,
        "output": [
            {
                "type": "message",
                "id": f"msg_{uuid.uuid4().hex[:8]}",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "usage": usage,
        "output_text": text,
        "status": "completed",
    }


# -----------------------------------------------------------------------------
# Responses output normalization
# -----------------------------------------------------------------------------
def _strip_reasoning_from_responses_output(resp_json: Dict[str, Any]) -> Dict[str, Any]:
    try:
        out = resp_json.get("output")
        if isinstance(out, list):
            new_out = []
            for item in out:
                if not isinstance(item, dict):
                    new_out.append(item)
                    continue
                if item.get("type") in ("reasoning", "reasoning_content"):
                    _metrics_inc("reasoning_stripped")
                    continue
                content = item.get("content")
                if isinstance(content, list):
                    new_content = []
                    removed = False
                    for part in content:
                        if isinstance(part, dict) and part.get("type") in ("reasoning", "reasoning_content"):
                            removed = True
                            continue
                        new_content.append(part)
                    if removed:
                        _metrics_inc("reasoning_stripped")
                    item = dict(item)
                    item["content"] = new_content
                new_out.append(item)
            resp_json["output"] = new_out
        if "reasoning" in resp_json:
            resp_json.pop("reasoning", None)
    except Exception:
        pass
    return resp_json


def _normalize_responses_output_text_blocks(resp_json: Dict[str, Any]) -> Dict[str, Any]:
    try:
        out = resp_json.get("output")
        if isinstance(out, list):
            for item in out:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            part["type"] = "output_text"
                            part.setdefault("annotations", [])
        ot = resp_json.get("output_text")
        if isinstance(ot, str) and ot.strip():
            if not isinstance(out, list) or not out:
                resp_json["output"] = [{
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": ot.strip(), "annotations": []}]
                }]
    except Exception:
        pass
    return resp_json


# -----------------------------------------------------------------------------
# SSE chat completion parser
# -----------------------------------------------------------------------------
def _chat_completion_from_sse(text: str, model: str) -> Dict[str, Any]:
    content = ""
    tool_calls = []
    usage = {}
    last_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
    created = int(time.time())
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)
        except Exception:
            continue
        if isinstance(obj, dict):
            last_id = obj.get("id") or last_id
            created = obj.get("created") or created
            try:
                u = _extract_usage(obj)
                if u:
                    usage = u
            except Exception:
                pass
            choices = obj.get("choices") or []
            if choices and isinstance(choices[0], dict):
                delta = choices[0].get("delta") or {}
                if isinstance(delta, dict):
                    tc = delta.get("tool_calls")
                    if isinstance(tc, list):
                        tool_calls = tc
                    piece = delta.get("content")
                    if isinstance(piece, str):
                        content += piece
    finish_reason = "stop"
    resp = {
        "id": last_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}
        ],
        "usage": usage,
    }
    if tool_calls:
        resp["choices"][0]["message"]["tool_calls"] = tool_calls
        resp["choices"][0]["message"]["content"] = ""
        resp = _ensure_toolcall_finish_reason(resp)
    resp = _strip_silent_reply(resp)
    return resp
