"""
title: Graphiti Knowledge Graph Pipe (Gateway Fast/Code/Extreme)
author: MAB + Navi
version: 2.8.0-owui
"""

from typing import Optional, Callable, Awaitable, List, Dict, Any, Tuple
from pydantic import BaseModel, Field
import requests
import json
from datetime import datetime


def extract_event_info(event_emitter) -> Tuple[Optional[str], Optional[str]]:
    """Extract chat_id and message_id from event emitter closure."""
    try:
        if not event_emitter or not getattr(event_emitter, "__closure__", None):
            return None, None
        for cell in event_emitter.__closure__:
            cc = cell.cell_contents
            if isinstance(cc, dict):
                return cc.get("chat_id"), cc.get("message_id")
    except Exception:
        pass
    return None, None


class Pipe:
    class Valves(BaseModel):
        graphiti_url: str = Field(
            default="http://host.docker.internal:8001", description="Graphiti API base URL"
        )
        gateway_url: str = Field(
            default="http://host.docker.internal:5050/v1",
            description="AI gateway base URL",
        )
        model: str = Field(
            default="fast",
            description="Default gateway model id (fast | code | extreme)",
        )

        search_limit: int = Field(
            default=10, description="Max Graphiti facts to retrieve"
        )
        enable_knowledge_injection: bool = Field(
            default=True, description="Inject Graphiti facts into user prompt"
        )
        context_pairs: int = Field(default=5, description="Message pairs to keep")

        enable_tools: bool = Field(default=True, description="Allow tool calling")
        max_tool_iterations: int = Field(default=4, description="Max tool loops")
        verbose_status: bool = Field(default=True, description="Emit status lines")

    def __init__(self):
        self.type = "pipe"
        self.id = "graphiti_pipe"
        self.name = "Graphiti Knowledge Graph"
        self.valves = self.Valves()
        self._last_usage: Dict[str, int] = {}

    def _normalize_model_name(self, model_name: Any) -> str:
        if not isinstance(model_name, str):
            return ""
        m = model_name.strip()
        if not m:
            return ""
        aliases = {
            "gpt-oss:20b": "fast",
            "ministral-3-14b (fast)": "fast",
            "ministral-3-14b": "fast",
            "ministral-3-14b-instruct-2512": "fast",
            "mistral-small-3.2-24b (general)": "extreme",
            "mistral-small-3.2-24b": "extreme",
            "mistral-small-3.2-24b-instruct-2506": "extreme",
            "devstral-small-2-24b (code)": "code",
            "devstral-small-2-24b": "code",
            "devstral-small-2-24b-instruct-2512": "code",
        }
        return aliases.get(m.lower(), m)

    def _resolve_model(self, body: Dict[str, Any]) -> str:
        allowed = {"fast", "code", "extreme"}
        body_model = self._normalize_model_name(body.get("model"))
        if body_model in allowed:
            return body_model
        valve_model = self._normalize_model_name(self.valves.model)
        if valve_model in allowed:
            return valve_model
        return "fast"

    # ----------------------------
    # OWUI background prompt guard
    # ----------------------------
    def is_owui_background_prompt(self, msg: str) -> bool:
        if not isinstance(msg, str):
            return False
        markers = [
            "Suggest 3-5 relevant follow-up",
            "Generate 1-3 broad tags",
            "Generate a concise title",
            "Return ONLY JSON",
            '"follow_ups"',
            '"tags"',
            '"title"',
            '"queries"',
        ]
        return any(m in msg for m in markers)

    async def emit_status(
        self, __event_emitter__, level: str, message: str, done: bool = False
    ):
        if not __event_emitter__ or not self.valves.verbose_status:
            return
        await __event_emitter__(
            {
                "type": "status",
                "data": {
                    "status": "complete" if done else "in_progress",
                    "level": level,
                    "description": message,
                    "done": done,
                },
            }
        )

    # ----------------------------
    # API key lookup (per-user)
    # ----------------------------
    def get_user_api_key(self, __user__: Optional[dict]) -> Optional[str]:
        try:
            if not __user__ or "settings" not in __user__:
                return None
            settings = __user__.get("settings", {})
            if isinstance(settings, str):
                settings = json.loads(settings)
            keys = (
                settings.get("ui", {})
                .get("directConnections", {})
                .get("OPENAI_API_KEYS", [])
            )
            return keys[0] if (keys and isinstance(keys, list)) else None
        except Exception:
            return None

    # ----------------------------
    # Graphiti calls
    # ----------------------------
    def graphiti_headers(self, api_key: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def search_graphiti(self, group_id: str, query: str, api_key: str) -> List[str]:
        try:
            r = requests.post(
                f"{self.valves.graphiti_url}/search",
                json={
                    "query": query,
                    "group_ids": [group_id],
                    "max_facts": self.valves.search_limit,
                },
                headers=self.graphiti_headers(api_key),
                timeout=10,
            )
            if r.status_code != 200:
                return []
            data = r.json() if r.content else {}
            facts: List[str] = []
            if isinstance(data, dict):
                fact_items = data.get("facts")
                if isinstance(fact_items, list):
                    for item in fact_items:
                        if isinstance(item, dict):
                            fact = item.get("fact")
                        else:
                            fact = item if isinstance(item, str) else None
                        if isinstance(fact, str):
                            f = fact.strip()
                            if len(f) >= 8:
                                facts.append(f)
                # Backward compatibility with older Graphiti responses.
                if not facts:
                    edges = data.get("edges", [])
                    if isinstance(edges, list):
                        for edge in edges:
                            if not isinstance(edge, dict):
                                continue
                            fact = edge.get("fact")
                            if isinstance(fact, str):
                                f = fact.strip()
                                if len(f) >= 8:
                                    facts.append(f)
            return facts
        except Exception:
            return []

    def store_in_graphiti(
        self, group_id: str, user_msg: str, asst_msg: str, api_key: str
    ) -> None:
        try:
            msgs = [
                {
                    "content": user_msg,
                    "role": "user",
                    "role_type": "user",
                    "timestamp": datetime.utcnow().isoformat() + "+00:00",
                }
            ]
            if asst_msg:
                msgs.append(
                    {
                        "content": asst_msg,
                        "role": "assistant",
                        "role_type": "assistant",
                        "timestamp": datetime.utcnow().isoformat() + "+00:00",
                    }
                )
            payload = {"group_id": group_id, "messages": msgs}
            requests.post(
                f"{self.valves.graphiti_url}/messages",
                json=payload,
                headers=self.graphiti_headers(api_key),
                timeout=10,
            )
        except Exception:
            pass

    # ----------------------------
    # Tools (OWUI -> OpenAI tools)
    # ----------------------------
    def convert_tools_to_openai_format(self, __tools__):
        if not __tools__:
            return []
        out: List[Dict[str, Any]] = []
        for name, t in (__tools__ or {}).items():
            if not isinstance(t, dict):
                continue
            spec = t.get("spec") or {}
            if not isinstance(spec, dict):
                continue
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": spec.get("name", name),
                        "description": spec.get("description", ""),
                        "parameters": spec.get("parameters", {}),
                    },
                }
            )
        return out

    async def execute_tool(
        self, tool_call: Dict[str, Any], __tools__: Optional[dict]
    ) -> str:
        try:
            fn = tool_call.get("function") or {}
            name = fn.get("name")
            if isinstance(name, str) and name.startswith("functions/"):
                name = name.split("/", 1)[1]
            args = fn.get("arguments", "{}")

            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}

            tool = (__tools__ or {}).get(name)
            if not tool and isinstance(name, str):
                # Fallback lookup by declared tool spec name.
                for _, candidate in (__tools__ or {}).items():
                    if not isinstance(candidate, dict):
                        continue
                    spec = candidate.get("spec") or {}
                    spec_name = spec.get("name") if isinstance(spec, dict) else None
                    if spec_name == name:
                        tool = candidate
                        break
            if not tool or "callable" not in tool:
                return json.dumps({"error": f"Tool '{name}' not found/invalid"})

            result = await tool["callable"](**(args if isinstance(args, dict) else {}))
            if isinstance(result, (dict, list)):
                return json.dumps(result)
            if isinstance(result, str):
                return result
            return json.dumps({"result": str(result)})
        except Exception as e:
            return json.dumps({"error": str(e)})

    # ----------------------------
    # Streaming parser (robust)
    # ----------------------------
    def _extract_stream_piece(
        self, chunk: Dict[str, Any]
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Returns (content_piece, tool_calls_piece)
        Supports OpenAI delta schema + message schema + fallback.
        """
        try:
            choices = chunk.get("choices") or []
            if not choices or not isinstance(choices[0], dict):
                return "", []

            c0 = choices[0]
            delta = c0.get("delta") or {}

            if isinstance(delta, dict):
                tool_calls = delta.get("tool_calls") or []
                content = delta.get("content") or ""
                return (
                    (content if isinstance(content, str) else ""),
                    (tool_calls if isinstance(tool_calls, list) else []),
                )

            msg = c0.get("message") or {}
            if isinstance(msg, dict):
                mc = msg.get("content")
                if isinstance(mc, str) and mc:
                    return mc, []

            txt = c0.get("text")
            if isinstance(txt, str) and txt:
                return txt, []

        except Exception:
            pass

        return "", []

    def _trim_context(self, msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        # Keep last N pairs (user+assistant) at minimum 2 messages
        try:
            max_msgs = max(2, int(self.valves.context_pairs) * 2)
            if len(msgs) > max_msgs:
                return msgs[-max_msgs:]
        except Exception:
            pass
        return msgs

    def _usage_from_headers(self, headers: Dict[str, str]) -> Dict[str, int]:
        try:
            p = headers.get("X-Usage-Prompt-Tokens")
            c = headers.get("X-Usage-Completion-Tokens")
            t = headers.get("X-Usage-Total-Tokens")
            out: Dict[str, int] = {}
            if p is not None:
                out["prompt_tokens"] = int(p)
            if c is not None:
                out["completion_tokens"] = int(c)
            if t is not None:
                out["total_tokens"] = int(t)
            return out
        except Exception:
            return {}

    def _format_usage(self, usage: Dict[str, int]) -> str:
        if not usage:
            return ""
        p = usage.get("prompt_tokens")
        c = usage.get("completion_tokens")
        t = usage.get("total_tokens")
        parts = []
        if isinstance(p, int):
            parts.append(f"prompt {p}")
        if isinstance(c, int):
            parts.append(f"completion {c}")
        if isinstance(t, int):
            parts.append(f"total {t}")
        return ", ".join(parts)

    def _extract_error_message(self, resp: requests.Response) -> str:
        try:
            if "application/json" in (resp.headers.get("Content-Type") or ""):
                data = resp.json()
                if isinstance(data, dict):
                    err = data.get("error") or {}
                    if isinstance(err, dict):
                        msg = err.get("message") or err.get("code") or "Gateway error"
                        hint = err.get("hint")
                        details = err.get("details")
                        parts = [str(msg)]
                        if hint:
                            parts.append(f"Hint: {hint}")
                        if details:
                            parts.append(f"Details: {details}")
                        return " | ".join(parts)
        except Exception:
            pass
        try:
            return resp.text[:500]
        except Exception:
            return "Gateway error"

    # ----------------------------
    # Main
    # ----------------------------
    async def pipe(
        self,
        body: dict,
        __user__=None,
        __event_emitter__=None,
        __event_call__=None,
        __tools__=None,
    ) -> Optional[dict]:

        api_key = self.get_user_api_key(__user__)
        if not api_key:
            await self.emit_status(
                __event_emitter__, "error", "Missing API key.", done=True
            )
            return {
                "error": "Missing API key. Set it in Settings → Connections → Manage Direct Connections."
            }

        messages = body.get("messages") or []
        if not messages:
            return {"error": "No messages provided"}

        user_message = (messages[-1].get("content") or "").strip()

        # OWUI meta prompts should not hit Graphiti/LLM
        if self.is_owui_background_prompt(user_message):
            msg = user_message or ""
            if "Generate a concise title" in msg or '"title"' in msg:
                return {"title": "Chat"}
            if "tags" in msg or '"tags"' in msg:
                return {"tags": []}
            if "follow-up" in msg or "follow up" in msg or '"follow_ups"' in msg:
                return {"follow_ups": []}
            if '"queries"' in msg:
                return {"queries": []}
            return {}

        chat_id, _ = extract_event_info(__event_emitter__)
        group_id = chat_id or "default_session"

        context_messages = list(messages)

        # Knowledge injection
        if self.valves.enable_knowledge_injection and user_message:
            await self.emit_status(__event_emitter__, "info", "Graphiti: searching…")
            facts = self.search_graphiti(group_id, user_message, api_key)
            if facts:
                inject = "\n".join(f"- {f}" for f in facts[:5])
                context_messages[-1][
                    "content"
                ] = f"[Graphiti context]\n{inject}\n\n{user_message}"

        context_messages = self._trim_context(context_messages)

        tools = (
            self.convert_tools_to_openai_format(__tools__)
            if (self.valves.enable_tools and __tools__)
            else []
        )

        iteration = 0
        final_text = ""

        user_email = None
        user_id = None
        user_name = None
        if isinstance(__user__, dict):
            user_email = __user__.get("email")
            user_id = __user__.get("id")
            user_name = __user__.get("name")

        active_model = self._resolve_model(body)

        while iteration <= self.valves.max_tool_iterations:
            await self.emit_status(
                __event_emitter__, "info", f"LLM: {active_model} (stream)"
            )

            payload: Dict[str, Any] = {
                "model": active_model,
                "messages": context_messages,
                "stream": True,
            }
            for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
                val = body.get(key)
                if isinstance(val, (int, float)):
                    payload[key] = val
            max_tokens = body.get("max_tokens")
            if isinstance(max_tokens, int) and max_tokens > 0:
                payload["max_tokens"] = max_tokens
            if tools and iteration == 0:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            if user_email:
                headers["x-openwebui-user-email"] = user_email
                headers["x-user-email"] = user_email
            if user_id:
                headers["x-openwebui-user-id"] = str(user_id)
            if user_name:
                headers["x-openwebui-user-name"] = user_name
            if chat_id:
                headers["x-openwebui-chat-id"] = str(chat_id)

            current_text = ""
            tool_buf: Dict[str, Dict[str, Any]] = {}

            with requests.post(
                f"{self.valves.gateway_url}/chat/completions",
                json=payload,
                headers=headers,
                stream=True,
                timeout=300,
            ) as resp:

                usage_from_headers = self._usage_from_headers(resp.headers)
                if usage_from_headers:
                    self._last_usage = usage_from_headers

                if resp.status_code != 200:
                    await self.emit_status(
                        __event_emitter__,
                        "error",
                        f"Gateway error {resp.status_code}",
                        done=True,
                    )
                    return {"error": self._extract_error_message(resp)}

                for raw in resp.iter_lines(decode_unicode=True):
                    if not raw:
                        continue
                    line = raw.strip()
                    if not line.startswith("data:"):
                        continue

                    data = line[5:].strip()
                    if data == "[DONE]":
                        break

                    try:
                        chunk = json.loads(data)
                    except Exception:
                        continue

                    if isinstance(chunk, dict) and chunk.get("type") == "usage":
                        usage = chunk.get("usage")
                        if isinstance(usage, dict):
                            self._last_usage = usage
                            msg = self._format_usage(usage)
                            if msg:
                                await self.emit_status(
                                    __event_emitter__, "info", f"Usage: {msg}"
                                )
                        continue

                    content, tc_piece = self._extract_stream_piece(chunk)

                    if tc_piece and isinstance(tc_piece, list):
                        for tc in tc_piece:
                            if not isinstance(tc, dict):
                                continue
                            tcid = tc.get("id") or str(tc.get("index") or len(tool_buf))
                            existing = tool_buf.get(
                                tcid,
                                {
                                    "id": tcid,
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                },
                            )
                            fn_new = tc.get("function") or {}
                            fn_old = existing.get("function") or {}

                            name_new = fn_new.get("name")
                            if isinstance(name_new, str) and name_new:
                                fn_old["name"] = name_new

                            args_new = fn_new.get("arguments")
                            if isinstance(args_new, str) and args_new:
                                fn_old["arguments"] = (
                                    fn_old.get("arguments") or ""
                                ) + args_new

                            existing["function"] = fn_old
                            tool_buf[tcid] = existing

                    if content:
                        current_text += content
                        if __event_emitter__:
                            await __event_emitter__(
                                {"type": "message", "data": {"content": content}}
                            )

            collected_tool_calls = list(tool_buf.values())

            if collected_tool_calls and __tools__ and self.valves.enable_tools:
                await self.emit_status(
                    __event_emitter__,
                    "info",
                    f"Tools: executing {len(collected_tool_calls)}…",
                )

                context_messages.append(
                    {
                        "role": "assistant",
                        "content": current_text or "",
                        "tool_calls": collected_tool_calls,
                    }
                )

                for tc in collected_tool_calls:
                    res = await self.execute_tool(tc, __tools__)
                    context_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": res,
                        }
                    )

                context_messages = self._trim_context(context_messages)
                iteration += 1
                continue

            final_text = current_text
            break

        await self.emit_status(
            __event_emitter__, "info", "Graphiti: storing…", done=True
        )
        self.store_in_graphiti(group_id, user_message, final_text, api_key)

        return None
