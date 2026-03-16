import time
from typing import Any, Dict, List

import httpx
from fastapi import Body, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from auth_mw import _token_ok_local


def register_aux_routes(app: FastAPI, deps: Dict[str, Any]) -> None:
    _metrics_inc = deps["_metrics_inc"]
    _error_response = deps["_error_response"]
    _invokeai_generate = deps["_invokeai_generate"]
    _sdxl_generate_comfy = deps["_sdxl_generate_comfy"]
    _sdxl_generate_auto1111 = deps["_sdxl_generate_auto1111"]
    SD_BACKEND = deps["SD_BACKEND"]
    SD_URL = deps["SD_URL"]
    SD_TIMEOUT = deps["SD_TIMEOUT"]
    _estimate_tokens = deps["_estimate_tokens"]
    _update_usage_metrics = deps["_update_usage_metrics"]
    _attach_usage_headers = deps["_attach_usage_headers"]
    ALLOWED_IMAGE_MODELS = deps["ALLOWED_IMAGE_MODELS"]
    ALLOWED_CHAT_MODELS = deps["ALLOWED_CHAT_MODELS"]
    ALLOWED_EMBED_MODELS = deps["ALLOWED_EMBED_MODELS"]
    CHAT_UPSTREAM = deps["CHAT_UPSTREAM"]
    extract_token_from_auth_header = deps["extract_token_from_auth_header"]
    _metrics_snapshot = deps["_metrics_snapshot"]
    _record_feedback = deps["_record_feedback"]

    @app.get("/internal/auth/validate")
    async def internal_auth_validate(request: Request):
        token = ""
        x_api_key = request.headers.get("x-api-key", "")
        if isinstance(x_api_key, str) and x_api_key.strip():
            token = x_api_key.strip()
        else:
            auth_header = request.headers.get("authorization", "")
            token = extract_token_from_auth_header(auth_header)

        if not token:
            return JSONResponse(status_code=401, content={"error": "missing token"})

        ok, why = _token_ok_local(token)
        if not ok:
            status_code = 429 if "quota" in why else 401
            return JSONResponse(status_code=status_code, content={"error": why})

        return {"status": "ok"}

    @app.get("/v1/sync")
    async def compatibility_sync():
        # Compatibility endpoint for clients that probe sync health/capabilities.
        return {"status": "ok", "synced": True, "ts": int(time.time())}

    @app.post("/v1/indexing/cache")
    async def compatibility_indexing_cache(body: dict = Body(default={})):
        # Compatibility endpoint for clients that opportunistically cache indexing hints.
        entries = 0
        if isinstance(body, dict):
            if isinstance(body.get("items"), list):
                entries = len(body.get("items") or [])
            elif isinstance(body.get("paths"), list):
                entries = len(body.get("paths") or [])
        return {"status": "ok", "cached": entries}

    @app.post("/v1/images/generations")
    async def images_generations(body: dict = Body(...)):
        _metrics_inc("image_requests")
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return _error_response(400, "missing_prompt", "Missing prompt.", "Provide a prompt string in the request body.")

        n = int(body.get("n") or 1)
        size = body.get("size")
        neg = body.get("negative_prompt")
        seed = body.get("seed")
        steps = body.get("steps")
        cfg = body.get("cfg_scale")

        try:
            if SD_BACKEND == "invokeai":
                images_b64 = await _invokeai_generate(prompt, n, size, neg, seed, steps, cfg)
            elif SD_BACKEND == "comfy":
                images_b64 = await _sdxl_generate_comfy(prompt, n, size, neg, seed, steps, cfg)
            else:
                images_b64 = await _sdxl_generate_auto1111(prompt, n, size, neg, seed, steps, cfg)
            _metrics_inc("image_success")
        except httpx.HTTPError as e:
            _metrics_inc("image_failed")
            return _error_response(502, "sd_backend_http", "Image backend request failed.", f"Check SD backend at {SD_URL} and retry.", str(e))
        except Exception as e:
            _metrics_inc("image_failed")
            return _error_response(500, "sd_backend_error", "Image backend error.", f"Check SD backend at {SD_URL} and retry.", str(e))

        now = int(time.time())
        prompt_tokens = _estimate_tokens(prompt + ("\n" + neg if isinstance(neg, str) and neg.strip() else ""))
        usage = {"prompt_tokens": prompt_tokens, "completion_tokens": 0, "total_tokens": prompt_tokens * max(1, n)}
        _update_usage_metrics(usage)

        resp = {"created": now, "data": [{"b64_json": b} for b in images_b64[:n]], "usage": usage}
        headers: Dict[str, str] = {}
        headers = _attach_usage_headers(headers, usage)
        return JSONResponse(content=resp, headers=headers)

    @app.get("/v1/models")
    async def list_models(request: Request):
        models: List[Dict[str, Any]] = []

        try:
            fwd_headers = {}
            auth = request.headers.get("authorization")
            if auth:
                fwd_headers["authorization"] = auth
            async with httpx.AsyncClient(base_url=CHAT_UPSTREAM, timeout=10) as client:
                r = await client.get("/v1/models", headers=fwd_headers)
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, dict) and isinstance(data.get("data"), list):
                        for m in data["data"]:
                            mid = m.get("id") if isinstance(m, dict) else None
                            if mid and (mid in ALLOWED_CHAT_MODELS or mid in ALLOWED_EMBED_MODELS or mid in ALLOWED_IMAGE_MODELS):
                                models.append(m)
        except Exception:
            pass

        if "sdxl-1.0" in ALLOWED_IMAGE_MODELS:
            models.append(
                {
                    "id": "sdxl-1.0",
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "stable-diffusion",
                    "permission": [],
                    "root": "stable-diffusion",
                }
            )

        return {"object": "list", "data": models}

    @app.get("/v1/metrics")
    async def metrics_endpoint():
        return _metrics_snapshot()

    @app.post("/v1/feedback")
    async def feedback_endpoint(request: Request, body: dict = Body(...)):
        message = (body.get("message") or "").strip()
        if not message:
            return _error_response(400, "missing_message", "Missing feedback message.")
        ftype = (body.get("type") or "general").strip() or "general"
        context = body.get("context")
        user_email = request.headers.get("x-openwebui-user-email") or request.headers.get("x-user-email")
        try:
            fid = await _record_feedback(user_email, ftype, message, context)
        except Exception as e:
            return _error_response(500, "feedback_error", "Failed to record feedback.", "Please retry later.", str(e))
        return {"status": "received", "id": fid}

    @app.post("/sdapi/v1/txt2img")
    async def a1111_txt2img(request: Request):
        import logging

        logger = logging.getLogger(__name__)
        logger.info(f"txt2img headers: {dict(request.headers)}")

        body_bytes = await request.body()
        headers = {"Content-Type": "application/json"}

        user_email = request.headers.get("x-openwebui-user-email")
        auth_header = request.headers.get("authorization", "")

        service_key = extract_token_from_auth_header(auth_header)

        if user_email:
            request.state.metering_user_email = user_email
            request.state.metering_service_key = service_key

        try:
            async with httpx.AsyncClient(timeout=SD_TIMEOUT) as client:
                resp = await client.post(
                    f"{SD_URL}/sdapi/v1/txt2img",
                    content=body_bytes,
                    headers=headers,
                )
                return Response(
                    content=resp.content,
                    status_code=resp.status_code,
                    media_type="application/json",
                )
        except httpx.HTTPError as e:
            return JSONResponse(status_code=502, content={"error": f"bridge error: {str(e)}"})
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": f"proxy error: {str(e)}"})

    @app.get("/sdapi/v1/sd-models")
    async def a1111_sd_models():
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{SD_URL}/sdapi/v1/sd-models")
                return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
        except httpx.HTTPError as e:
            return JSONResponse(status_code=502, content={"error": f"bridge error: {str(e)}"})
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": f"proxy error: {str(e)}"})

    @app.get("/sdapi/v1/samplers")
    async def a1111_samplers():
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{SD_URL}/sdapi/v1/samplers")
                return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
        except httpx.HTTPError as e:
            return JSONResponse(status_code=502, content={"error": f"bridge error: {str(e)}"})
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": f"proxy error: {str(e)}"})

    @app.get("/sdapi/v1/options")
    async def a1111_get_options():
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{SD_URL}/sdapi/v1/options")
                return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
        except httpx.HTTPError as e:
            return JSONResponse(status_code=502, content={"error": f"bridge error: {str(e)}"})
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": f"proxy error: {str(e)}"})

    @app.post("/sdapi/v1/options")
    async def a1111_set_options(request: Request):
        body_bytes = await request.body()
        headers = {"Content-Type": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{SD_URL}/sdapi/v1/options",
                    content=body_bytes,
                    headers=headers,
                )
                return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
        except httpx.HTTPError as e:
            return JSONResponse(status_code=502, content={"error": f"bridge error: {str(e)}"})
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": f"proxy error: {str(e)}"})
