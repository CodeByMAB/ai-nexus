# /opt/ai/gateway/sdxl_backend.py  — SDXL image generation backends (Auto1111, ComfyUI, InvokeAI)
import asyncio
import base64
import json
import random
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

# -----------------------------------------------------------------------------
# Module-level config (set via configure())
# -----------------------------------------------------------------------------
_SD_URL = "http://127.0.0.1:7860"
_SD_TIMEOUT = 120.0
_INVOKEAI_DB_PATH = "${HOME}/invokeai/databases/invokeai.db"
_INVOKEAI_QUEUE_ID = "default"
_INVOKEAI_POLL_INTERVAL = 1.0
_INVOKEAI_POLL_TIMEOUT = 120.0


def configure(
    sd_url: str,
    sd_timeout: float,
    invokeai_db_path: str,
    invokeai_queue_id: str,
    invokeai_poll_interval: float,
    invokeai_poll_timeout: float,
) -> None:
    global _SD_URL, _SD_TIMEOUT, _INVOKEAI_DB_PATH, _INVOKEAI_QUEUE_ID
    global _INVOKEAI_POLL_INTERVAL, _INVOKEAI_POLL_TIMEOUT
    _SD_URL = sd_url
    _SD_TIMEOUT = sd_timeout
    _INVOKEAI_DB_PATH = invokeai_db_path
    _INVOKEAI_QUEUE_ID = invokeai_queue_id
    _INVOKEAI_POLL_INTERVAL = invokeai_poll_interval
    _INVOKEAI_POLL_TIMEOUT = invokeai_poll_timeout


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _parse_size(size: Optional[str]) -> Tuple[int, int]:
    default = (1024, 1024)
    if not size:
        return default
    try:
        w, h = size.lower().split("x")
        return (int(w), int(h))
    except Exception:
        return default


# -----------------------------------------------------------------------------
# Auto1111
# -----------------------------------------------------------------------------
async def _sdxl_generate_auto1111(
    prompt: str,
    n: int,
    size: Optional[str],
    neg: Optional[str],
    seed: Optional[int],
    steps: Optional[int],
    cfg: Optional[float],
) -> List[str]:
    width, height = _parse_size(size)
    payload = {
        "prompt": prompt,
        "negative_prompt": neg or "",
        "width": width,
        "height": height,
        "seed": seed if seed is not None else -1,
        "steps": steps or 25,
        "cfg_scale": cfg or 7.0,
        "n_iter": 1,
        "batch_size": n,
    }
    async with httpx.AsyncClient(timeout=_SD_TIMEOUT) as client:
        r = await client.post(f"{_SD_URL}/sdapi/v1/txt2img", json=payload)
        r.raise_for_status()
        data = r.json()
        b64s = data.get("images", [])
        cleaned = []
        for s in b64s:
            if isinstance(s, str) and s.strip().startswith("data:image"):
                parts = s.split(",", 1)
                if len(parts) > 1:
                    s = parts[1]
            cleaned.append(s)
        return cleaned


# -----------------------------------------------------------------------------
# ComfyUI
# -----------------------------------------------------------------------------
async def _sdxl_generate_comfy(
    prompt: str,
    n: int,
    size: Optional[str],
    neg: Optional[str],
    seed: Optional[int],
    steps: Optional[int],
    cfg: Optional[float],
) -> List[str]:
    width, height = _parse_size(size)
    graph = {
        "prompt": {
            "inputs": {
                "seed": seed or 0,
                "steps": steps or 25,
                "cfg": cfg or 7.0,
                "width": width,
                "height": height,
                "positive": prompt,
                "negative": neg or "",
                "samples": n,
            }
        }
    }
    async with httpx.AsyncClient(timeout=_SD_TIMEOUT) as client:
        r = await client.post(f"{_SD_URL}/api/generate", json=graph)
        r.raise_for_status()
        out = r.json()
        return out.get("images", [])


# -----------------------------------------------------------------------------
# InvokeAI (queue-based)
# -----------------------------------------------------------------------------
_INVOKEAI_GRAPH_CACHE: Dict[str, Any] = {}
_INVOKEAI_GRAPH_CACHE_TS = 0.0


def _invokeai_load_graph_template() -> Optional[Dict[str, Any]]:
    global _INVOKEAI_GRAPH_CACHE_TS
    try:
        now = time.time()
        if _INVOKEAI_GRAPH_CACHE and (now - _INVOKEAI_GRAPH_CACHE_TS) < 60:
            return json.loads(json.dumps(_INVOKEAI_GRAPH_CACHE))
        conn = sqlite3.connect(_INVOKEAI_DB_PATH)
        cur = conn.cursor()
        cur.execute("select session from session_queue where status='completed' order by updated_at desc limit 1")
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        session = json.loads(row[0])
        graph = session.get("graph")
        if isinstance(graph, dict):
            _INVOKEAI_GRAPH_CACHE.clear()
            _INVOKEAI_GRAPH_CACHE.update(graph)
            _INVOKEAI_GRAPH_CACHE_TS = now
            return json.loads(json.dumps(graph))
    except Exception:
        return None
    return None


def _invokeai_apply_params(
    graph: Dict[str, Any],
    prompt: str,
    negative: Optional[str],
    width: int,
    height: int,
    steps: Optional[int],
    cfg: Optional[float],
    seed: int,
) -> Dict[str, Any]:
    nodes = (graph or {}).get("nodes") or {}
    neg = negative or ""
    for nid, node in nodes.items():
        if not isinstance(node, dict):
            continue
        ntype = node.get("type")
        if "positive_prompt" in nid and ntype == "string":
            node["value"] = prompt
        if "neg_cond" in nid and ntype == "sdxl_compel_prompt":
            node["prompt"] = neg
            node["style"] = neg
        if "seed" in nid and ntype == "integer":
            node["value"] = seed
        if ntype == "noise":
            node["seed"] = seed
            node["width"] = width
            node["height"] = height
        if ntype == "denoise_latents":
            if steps is not None:
                node["steps"] = steps
            if cfg is not None:
                node["cfg_scale"] = cfg
        if ntype == "core_metadata":
            node["width"] = width
            node["height"] = height
            if steps is not None:
                node["steps"] = steps
            if cfg is not None:
                node["cfg_scale"] = cfg
            node["seed"] = seed
            node["positive_prompt"] = prompt
            node["negative_prompt"] = neg
    return graph


def _invokeai_collect_images_from_db(batch_id: str) -> List[str]:
    try:
        conn = sqlite3.connect(_INVOKEAI_DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "select session from session_queue where batch_id=? and status='completed' order by updated_at desc",
            (batch_id,),
        )
        rows = cur.fetchall()
        conn.close()
        images: List[str] = []
        for (sess_text,) in rows:
            try:
                session = json.loads(sess_text)
                results = session.get("results") or {}
                for r in results.values():
                    if isinstance(r, dict):
                        img = r.get("image") or {}
                        name = img.get("image_name") if isinstance(img, dict) else None
                        if isinstance(name, str):
                            images.append(name)
            except Exception:
                continue
        return images
    except Exception:
        return []


async def _invokeai_generate(
    prompt: str,
    n: int,
    size: Optional[str],
    neg: Optional[str],
    seed: Optional[int],
    steps: Optional[int],
    cfg: Optional[float],
) -> List[str]:
    width, height = _parse_size(size)
    base_graph = _invokeai_load_graph_template()
    if not base_graph:
        raise RuntimeError("InvokeAI graph template not found")

    runs = max(1, int(n or 1))
    chosen_seed = seed if seed is not None else random.randint(0, 2_147_483_647)
    graph = _invokeai_apply_params(base_graph, prompt, neg, width, height, steps, cfg, chosen_seed)

    body = {
        "batch": {
            "graph": graph,
            "runs": runs,
            "origin": "generate",
            "destination": "generate",
        },
        "prepend": False,
    }

    async with httpx.AsyncClient(timeout=_SD_TIMEOUT) as client:
        r = await client.post(f"{_SD_URL}/api/v1/queue/{_INVOKEAI_QUEUE_ID}/enqueue_batch", json=body)
        r.raise_for_status()
        data = r.json()
        batch = data.get("batch") or {}
        batch_id = batch.get("batch_id")
        if not batch_id:
            raise RuntimeError("InvokeAI enqueue returned no batch_id")

        # Poll queue status
        deadline = time.time() + _INVOKEAI_POLL_TIMEOUT
        while time.time() < deadline:
            s = await client.get(f"{_SD_URL}/api/v1/queue/{_INVOKEAI_QUEUE_ID}/b/{batch_id}/status")
            if s.status_code == 200:
                st = s.json()
                total = st.get("total", 0) or 0
                completed = st.get("completed", 0) or 0
                failed = st.get("failed", 0) or 0
                canceled = st.get("canceled", 0) or 0
                if total and (completed + failed + canceled) >= total:
                    break
            await asyncio.sleep(_INVOKEAI_POLL_INTERVAL)

        # Collect images from DB
        images = _invokeai_collect_images_from_db(batch_id)
        if not images:
            raise RuntimeError("InvokeAI did not return any images")

        # Fetch images as base64
        b64s: List[str] = []
        for name in images[:n]:
            img_resp = await client.get(f"{_SD_URL}/api/v1/images/i/{name}/full")
            img_resp.raise_for_status()
            b64s.append(base64.b64encode(img_resp.content).decode("utf-8"))
        return b64s
