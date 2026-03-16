# /opt/ai/gateway/memory_adapter.py  — Zep memory adapter (session/search/write)
from typing import Dict, List

import httpx

# -----------------------------------------------------------------------------
# Module-level config (set via configure())
# -----------------------------------------------------------------------------
_ZEP_URL = "http://127.0.0.1:8000"
_ZEP_API_KEY = ""


def configure(zep_url: str, zep_api_key: str) -> None:
    global _ZEP_URL, _ZEP_API_KEY
    _ZEP_URL = zep_url
    _ZEP_API_KEY = zep_api_key


def _zep_headers() -> Dict[str, str]:
    h = {"content-type": "application/json"}
    if _ZEP_API_KEY:
        h["authorization"] = f"Bearer {_ZEP_API_KEY}"
    return h


async def zep_upsert_session(client: httpx.AsyncClient, session_id: str) -> None:
    try:
        await client.post(
            f"{_ZEP_URL}/api/v1/sessions",
            headers=_zep_headers(),
            json={"session_id": session_id},
            timeout=5.0,
        )
    except Exception:
        pass


async def zep_search_memory(
    client: httpx.AsyncClient, session_id: str, query: str, top_k: int
) -> List[str]:
    try:
        resp = await client.get(
            f"{_ZEP_URL}/api/v1/sessions/{session_id}/memory",
            headers=_zep_headers(),
            timeout=6.0,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        messages = data.get("messages", [])
        out = []
        for msg in messages[-top_k:]:
            if isinstance(msg, dict):
                content = msg.get("content", "")
                role = msg.get("role", "")
                if content:
                    out.append(f"[{role}]: {content}")
        return out
    except Exception:
        return []


async def zep_write_messages(
    client: httpx.AsyncClient,
    session_id: str,
    messages: List[Dict[str, str]],
) -> None:
    try:
        zep_messages = []
        for msg in messages:
            role = msg.get("role", "user")
            zep_messages.append({
                "role": role,
                "role_type": role,
                "content": msg.get("content", "")
            })

        await client.post(
            f"{_ZEP_URL}/api/v1/sessions/{session_id}/memory",
            headers=_zep_headers(),
            json={"messages": zep_messages},
            timeout=6.0,
        )
    except Exception:
        pass
