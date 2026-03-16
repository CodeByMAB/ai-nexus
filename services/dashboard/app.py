"""
AI System Dashboard - Main FastAPI Application
"""

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import auth
import metrics

DATA_DIR = Path("/opt/ai/dashboard/data")
STATIC_DIR = Path("/opt/ai/dashboard/static")
ENV = os.environ.get("DASHBOARD_ENV", "development")
IS_PROD = ENV == "production"

app = FastAPI(
    title="AI Dashboard",
    docs_url=None,  # Disable Swagger in prod
    redoc_url=None,
    openapi_url=None,
)

# ---------------------------------------------------------------------------
# Security headers middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    if IS_PROD:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )
    return response


# ---------------------------------------------------------------------------
# CSRF / Origin check middleware
# ---------------------------------------------------------------------------

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
ALLOWED_ORIGINS = {
    os.environ.get("DASHBOARD_ORIGIN", "https://dash.YOURDOMAIN.COM"),
    "http://localhost:8200",
    "http://127.0.0.1:8200",
}

@app.middleware("http")
async def csrf_protection(request: Request, call_next):
    if request.method not in SAFE_METHODS and request.url.path.startswith("/auth"):
        origin = request.headers.get("origin", "")
        # Allow requests with no origin from same-origin forms
        if origin and origin not in ALLOWED_ORIGINS:
            return JSONResponse(
                status_code=403,
                content={"detail": "CSRF check failed: invalid origin"},
            )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Session helper
# ---------------------------------------------------------------------------

def get_current_user(session: Optional[str] = Cookie(None)) -> Optional[dict]:
    if not session:
        return None
    return auth.verify_session(session)


def require_auth(request: Request) -> dict:
    session_token = request.cookies.get("session")
    if not session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = auth.verify_session(session_token)
    if not user:
        raise HTTPException(status_code=401, detail="Session expired")
    return user


def set_session_cookie(response: Response, token: str):
    response.set_cookie(
        "session",
        token,
        httponly=True,
        secure=IS_PROD,
        samesite="strict",
        max_age=28800,  # 8 hours
        path="/",
    )


def clear_session_cookie(response: Response):
    response.delete_cookie("session", path="/", samesite="strict")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class SetupRequest(BaseModel):
    email: str
    password: str
    totp_code: str
    totp_secret: str


class LoginRequest(BaseModel):
    email: str
    password: str


class TOTPVerifyRequest(BaseModel):
    email: str
    code: str


class WebAuthnCompleteRequest(BaseModel):
    credential: str
    name: Optional[str] = "Security Key"


class WebAuthnAuthCompleteRequest(BaseModel):
    email: str
    credential: str


class DeleteCredentialRequest(BaseModel):
    credential_id: str


class ModeRequest(BaseModel):
    mode: str


class ServiceControlRequest(BaseModel):
    action: str        # start | stop | restart
    service: str
    service_type: str  # systemd | docker


class KeyCreateRequest(BaseModel):
    client_name: str
    client_email: str
    plan_type: str = "custom"
    monthly_token_allowance: int = 500000


class KeyUpdateRequest(BaseModel):
    is_active: Optional[bool] = None
    monthly_token_allowance: Optional[int] = None
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Setup endpoints
# ---------------------------------------------------------------------------

@app.get("/auth/setup-status")
async def setup_status():
    return {"setup_complete": auth.user_db.has_users()}


@app.post("/auth/setup/begin")
async def setup_begin(request: Request):
    """Step 1 of setup: generate TOTP secret and QR code."""
    if auth.user_db.has_users():
        raise HTTPException(status_code=403, detail="Setup already complete")

    ip = request.client.host if request.client else "unknown"
    if not auth.check_rate_limit(ip):
        raise HTTPException(status_code=429, detail="Too many attempts")

    body = await request.json()
    email = body.get("email", "").strip().lower()
    password = body.get("password", "")

    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    totp_secret = auth.generate_totp_secret()
    qr_b64 = auth.generate_totp_qr_base64(totp_secret, email)
    totp_uri = auth.get_totp_uri(totp_secret, email)

    return {
        "totp_secret": totp_secret,
        "totp_uri": totp_uri,
        "qr_code": qr_b64,
    }


@app.post("/auth/setup")
async def setup_complete(setup: SetupRequest, request: Request):
    """Step 2 of setup: verify TOTP and create user."""
    if auth.user_db.has_users():
        raise HTTPException(status_code=403, detail="Setup already complete")

    ip = request.client.host if request.client else "unknown"
    if not auth.check_rate_limit(ip):
        raise HTTPException(status_code=429, detail="Too many attempts")

    email = setup.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email")
    if len(setup.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    # Verify TOTP code
    if not auth.verify_totp(setup.totp_secret, setup.totp_code):
        raise HTTPException(status_code=400, detail="Invalid TOTP code — scan QR and try again")

    password_hash = auth.hash_password(setup.password)
    user = auth.user_db.create_user(email, password_hash, setup.totp_secret)
    auth.user_db.mark_totp_verified(email)

    response = JSONResponse({"success": True, "email": email})
    token = auth.create_session(user["id"], email)
    set_session_cookie(response, token)
    return response


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------

# Temporary pre-auth state: {email: {password_verified: bool}}
_pre_auth: dict[str, dict] = {}


@app.post("/auth/login")
async def login(login_req: LoginRequest, request: Request):
    ip = request.client.host if request.client else "unknown"
    if not auth.check_rate_limit(ip):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again in 15 minutes.")

    email = login_req.email.strip().lower()
    user = auth.user_db.get_user(email)

    if not user or not auth.verify_password(login_req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Store pre-auth state
    _pre_auth[email] = {
        "password_verified": True,
        "ip": ip,
    }

    return {
        "needs_totp": True,
        "has_webauthn": auth.has_webauthn_credentials(email),
        "email": email,
    }


@app.post("/auth/verify-2fa")
async def verify_2fa(verify_req: TOTPVerifyRequest, request: Request):
    ip = request.client.host if request.client else "unknown"
    if not auth.check_rate_limit(ip):
        raise HTTPException(status_code=429, detail="Too many attempts")

    email = verify_req.email.strip().lower()
    pre = _pre_auth.get(email)
    if not pre or not pre.get("password_verified"):
        raise HTTPException(status_code=401, detail="Please complete password login first")

    user = auth.user_db.get_user(email)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    if not auth.verify_totp(user["totp_secret"], verify_req.code):
        raise HTTPException(status_code=401, detail="Invalid TOTP code")

    # Clear pre-auth state
    _pre_auth.pop(email, None)

    response = JSONResponse({"success": True, "email": email})
    token = auth.create_session(user["id"], email)
    set_session_cookie(response, token)
    return response


@app.post("/auth/logout")
async def logout(request: Request):
    response = JSONResponse({"success": True})
    clear_session_cookie(response)
    return response


# ---------------------------------------------------------------------------
# WebAuthn endpoints
# ---------------------------------------------------------------------------

@app.get("/auth/webauthn/register/begin")
async def webauthn_register_begin(request: Request):
    user = require_auth(request)
    email = user["email"]
    user_record = auth.user_db.get_user(email)
    if not user_record:
        raise HTTPException(status_code=401, detail="User not found")

    options_json = auth.webauthn_begin_registration(email, user_record["id"])
    return Response(content=options_json, media_type="application/json")


@app.post("/auth/webauthn/register/complete")
async def webauthn_register_complete(req: WebAuthnCompleteRequest, request: Request):
    user = require_auth(request)
    email = user["email"]

    try:
        result = auth.webauthn_complete_registration(email, req.credential, req.name)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Registration failed: {str(e)}")


@app.post("/auth/webauthn/authenticate/begin")
async def webauthn_auth_begin(request: Request):
    body = await request.json()
    email = body.get("email", "").strip().lower()

    pre = _pre_auth.get(email)
    if not pre or not pre.get("password_verified"):
        raise HTTPException(status_code=401, detail="Please complete password login first")

    options_json = auth.webauthn_begin_authentication(email)
    if not options_json:
        raise HTTPException(status_code=404, detail="No WebAuthn credentials registered")

    return Response(content=options_json, media_type="application/json")


@app.post("/auth/webauthn/authenticate/complete")
async def webauthn_auth_complete(req: WebAuthnAuthCompleteRequest, request: Request):
    ip = request.client.host if request.client else "unknown"
    if not auth.check_rate_limit(ip):
        raise HTTPException(status_code=429, detail="Too many attempts")

    email = req.email.strip().lower()
    pre = _pre_auth.get(email)
    if not pre or not pre.get("password_verified"):
        raise HTTPException(status_code=401, detail="Please complete password login first")

    success = auth.webauthn_complete_authentication(email, req.credential)
    if not success:
        raise HTTPException(status_code=401, detail="WebAuthn authentication failed")

    _pre_auth.pop(email, None)

    user = auth.user_db.get_user(email)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    response = JSONResponse({"success": True, "email": email})
    token = auth.create_session(user["id"], email)
    set_session_cookie(response, token)
    return response


@app.get("/auth/webauthn/credentials")
async def list_webauthn_credentials(request: Request):
    user = require_auth(request)
    creds = auth.get_webauthn_credentials(user["email"])
    return {"credentials": creds}


@app.post("/auth/webauthn/credentials/delete")
async def delete_webauthn_credential(req: DeleteCredentialRequest, request: Request):
    user = require_auth(request)
    success = auth.delete_webauthn_credential(user["email"], req.credential_id)
    if not success:
        raise HTTPException(status_code=404, detail="Credential not found")
    return {"success": True}


# ---------------------------------------------------------------------------
# API endpoints (protected)
# ---------------------------------------------------------------------------

@app.get("/api/metrics")
async def get_metrics(request: Request):
    require_auth(request)
    return metrics.get_all_metrics()


@app.get("/api/stream")
async def stream_metrics(request: Request):
    require_auth(request)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                data = metrics.get_all_metrics()
                yield f"data: {json.dumps(data)}\n\n"
                await asyncio.sleep(3)
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )


LOG_SERVICES = {
    "vllm": ["journalctl", "-u", "vllm", "-n", "{lines}", "--no-pager", "-o", "short-iso"],
    "ai-gateway": ["journalctl", "-u", "ai-gateway", "-n", "{lines}", "--no-pager", "-o", "short-iso"],
    "openai-shim": ["journalctl", "-u", "openai-shim", "-n", "{lines}", "--no-pager", "-o", "short-iso"],
    "payment-webhook": ["journalctl", "-u", "payment-webhook", "-n", "{lines}", "--no-pager", "-o", "short-iso"],
    "ai-dashboard": ["journalctl", "-u", "ai-dashboard", "-n", "{lines}", "--no-pager", "-o", "short-iso"],
    "nginx": ["journalctl", "-u", "nginx", "-n", "{lines}", "--no-pager", "-o", "short-iso"],
}


@app.get("/api/logs")
async def get_logs(request: Request, service: str = "vllm", lines: int = 100):
    require_auth(request)

    if service not in LOG_SERVICES:
        raise HTTPException(status_code=400, detail=f"Unknown service: {service}")

    lines = min(max(lines, 10), 500)
    cmd = [part.replace("{lines}", str(lines)) for part in LOG_SERVICES[service]]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        log_lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
        return {
            "service": service,
            "lines": log_lines,
            "count": len(log_lines),
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Log fetch timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/me")
async def get_me(request: Request):
    user = require_auth(request)
    return {
        "email": user["email"],
        "has_webauthn": auth.has_webauthn_credentials(user["email"]),
    }


# ---------------------------------------------------------------------------
# System Control endpoints (protected, auth required)
# ---------------------------------------------------------------------------

ALLOWED_MODES = {"extreme", "code", "fast", "fast+image"}
CONTROLLABLE_SYSTEMD = {"vllm", "ai-gateway", "openai-shim", "payment-webhook"}
CONTROLLABLE_DOCKER = {"openwebui", "graphiti", "graphiti-neo4j", "zep", "zep-postgres"}


@app.post("/api/control/mode")
async def switch_ai_mode(req: ModeRequest, request: Request):
    require_auth(request)
    if req.mode not in ALLOWED_MODES:
        raise HTTPException(400, "Invalid mode")
    try:
        result = subprocess.run(
            ["sudo", "/opt/ai/vllm/scripts/switch-mode.sh", req.mode],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            raise HTTPException(500, f"Mode switch failed: {result.stderr[:200]}")
        return {"success": True, "mode": req.mode}
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Mode switch timed out")


@app.post("/api/control/service")
async def control_service(req: ServiceControlRequest, request: Request):
    require_auth(request)
    action = req.action.lower()
    if action not in {"start", "stop", "restart"}:
        raise HTTPException(400, "Action must be start, stop, or restart")
    if req.service_type == "systemd":
        if req.service not in CONTROLLABLE_SYSTEMD:
            raise HTTPException(400, "Service not controllable")
        cmd = ["sudo", "/usr/bin/systemctl", action, req.service]
    elif req.service_type == "docker":
        if req.service not in CONTROLLABLE_DOCKER:
            raise HTTPException(400, "Container not controllable")
        cmd = ["docker", action, req.service]
    else:
        raise HTTPException(400, "service_type must be systemd or docker")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise HTTPException(500, f"Failed: {result.stderr[:200]}")
        return {"success": True, "action": action, "service": req.service}
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Command timed out")


# ---------------------------------------------------------------------------
# API Key Management endpoints
# ---------------------------------------------------------------------------

@app.get("/api/keys")
async def list_keys(request: Request):
    require_auth(request)
    import sqlite3
    conn = sqlite3.connect(f"file:/opt/ai/keys/keys.sqlite?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT id, key_prefix, client_name, client_email, plan_type,
               monthly_token_allowance, monthly_tokens_remaining, purchased_tokens_remaining,
               tokens_used_this_month, is_active, created_at, expires_at, subscription_status, notes
        FROM api_keys ORDER BY created_at DESC LIMIT 200
    """)
    keys = [dict(row) for row in cur.fetchall()]
    conn.close()
    return {"keys": keys}


@app.post("/api/keys")
async def create_key(req: KeyCreateRequest, request: Request):
    require_auth(request)
    import sqlite3
    import secrets
    import hashlib
    raw_key = "pg_" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = raw_key[:10]
    conn = sqlite3.connect("/opt/ai/keys/keys.sqlite", timeout=5)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO api_keys (key_hash, key_prefix, client_name, client_email, plan_type,
            monthly_token_allowance, tokens_remaining, monthly_tokens_remaining, is_active)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
    """, (key_hash, key_prefix, req.client_name, req.client_email, req.plan_type,
          req.monthly_token_allowance, req.monthly_token_allowance, req.monthly_token_allowance))
    conn.commit()
    key_id = cur.lastrowid
    conn.close()
    return {"success": True, "id": key_id, "key": raw_key, "prefix": key_prefix}


@app.patch("/api/keys/{key_id}")
async def update_key(key_id: int, req: KeyUpdateRequest, request: Request):
    require_auth(request)
    import sqlite3
    conn = sqlite3.connect("/opt/ai/keys/keys.sqlite", timeout=5)
    cur = conn.cursor()
    updates = []
    params = []
    if req.is_active is not None:
        updates.append("is_active = ?")
        params.append(1 if req.is_active else 0)
    if req.monthly_token_allowance is not None:
        updates.append("monthly_token_allowance = ?")
        params.append(req.monthly_token_allowance)
        updates.append("monthly_tokens_remaining = ?")
        params.append(req.monthly_token_allowance)
    if req.notes is not None:
        updates.append("notes = ?")
        params.append(req.notes)
    if not updates:
        raise HTTPException(400, "Nothing to update")
    params.append(key_id)
    cur.execute(f"UPDATE api_keys SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()
    conn.close()
    return {"success": True}


@app.delete("/api/keys/{key_id}")
async def delete_key(key_id: int, request: Request):
    require_auth(request)
    import sqlite3
    conn = sqlite3.connect("/opt/ai/keys/keys.sqlite", timeout=5)
    cur = conn.cursor()
    cur.execute("UPDATE api_keys SET is_active = 0 WHERE id = ?", (key_id,))
    conn.commit()
    conn.close()
    return {"success": True}


# ---------------------------------------------------------------------------
# Static files and SPA
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def root(request: Request):
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        return HTMLResponse("<h1>Dashboard not initialized</h1>", status_code=500)
    return FileResponse(str(index_path))


@app.get("/{full_path:path}")
async def catch_all(full_path: str, request: Request):
    # Don't catch API paths
    if full_path.startswith(("api/", "auth/")):
        raise HTTPException(status_code=404, detail="Not found")
    index_path = STATIC_DIR / "index.html"
    return FileResponse(str(index_path))
