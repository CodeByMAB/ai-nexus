import os
import time
import sqlite3
import httpx
import hashlib
from typing import Callable, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

KEY_DB_PATH = os.getenv("KEY_DB_PATH", "/opt/ai/keys/keys.sqlite")
VALIDATE_URL = os.getenv("VALIDATE_URL", "")
ENFORCE_KEYS = os.getenv("ENFORCE_KEYS", "1")            # "1" to require Authorization header
USE_REMOTE_VALIDATE = os.getenv("USE_REMOTE_VALIDATE", "0")  # "1" to call VALIDATE_URL
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")               # optional override that always passes

def _db():
    conn = sqlite3.connect(KEY_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    return conn

def _ensure_usage_schema():
    with _db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS api_usage(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          token TEXT NOT NULL,
          endpoint TEXT NOT NULL,
          ts INTEGER NOT NULL,
          req_bytes INTEGER,
          rsp_bytes INTEGER,
          prompt_tokens INTEGER,
          completion_tokens INTEGER,
          user_email TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_usage_token_ts ON api_usage(token, ts);
        """)
_ensure_usage_schema()

def _token_ok_local(token: str) -> tuple[bool, str]:
    """
    Validate a pg_ token against api_keys and check token balance.
    Uses new token accounting system: monthly_tokens_remaining + purchased_tokens_remaining
    """
    if not token.startswith("pg_"):
        return False, "invalid token format"
    key_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with _db() as c:
        row = c.execute("""
            SELECT
                client_email,
                is_active,
                subscription_status,
                monthly_tokens_remaining,
                purchased_tokens_remaining,
                monthly_reset_date,
                expires_at
            FROM api_keys
            WHERE key_hash = ?
        """, (key_hash,)).fetchone()

        if not row:
            return False, "invalid token"

        if row["is_active"] != 1:
            return False, "token disabled"

        # Check if subscription is active (if it has a subscription)
        sub_status = row["subscription_status"]
        if sub_status and sub_status not in ("active", "trialing"):
            return False, f"subscription {sub_status}"

        # Check if token has expired
        expires_at = row["expires_at"]
        if expires_at:
            from datetime import datetime
            expiry = datetime.fromisoformat(expires_at)
            if datetime.now() > expiry:
                return False, "token expired"

        # Check token balance (combined pool)
        monthly_remaining = row["monthly_tokens_remaining"] or 0
        purchased_remaining = row["purchased_tokens_remaining"] or 0
        total_tokens = monthly_remaining + purchased_remaining

        if total_tokens <= 0:
            return False, "insufficient tokens"

        # Legacy daily/monthly limit support (if set)
        # Check if columns exist in row (they may not be in the schema)
        try:
            daily_limit = row["daily_limit"] if "daily_limit" in row.keys() else None
            monthly_limit = row["monthly_limit"] if "monthly_limit" in row.keys() else None
        except (KeyError, IndexError):
            daily_limit = None
            monthly_limit = None

        if daily_limit or monthly_limit:
            now = int(time.time())
            day_start = now - (now % 86400)
            day_count = c.execute(
                "SELECT COUNT(*) AS n FROM api_usage WHERE token=? AND ts>=?",
                (token, day_start)
            ).fetchone()["n"]
            month_count = c.execute(
                "SELECT COUNT(*) AS n FROM api_usage WHERE token=? AND ts>=?",
                (token, now - 30 * 86400)
            ).fetchone()["n"]

            if daily_limit is not None and day_count >= daily_limit:
                return False, "daily quota exceeded"
            if monthly_limit is not None and month_count >= monthly_limit:
                return False, "monthly quota exceeded"

        return True, ""

def _parse_int_header(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None

class RequireBearerAndMeter(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable):
        # Don't guard CORS preflight requests
        if request.method == "OPTIONS":
            return await call_next(request)

        # Guard API paths
        # /v1/* - OpenAI-style endpoints (require auth)
        # /sdapi/v1/txt2img, /sdapi/v1/img2img - Image generation (require auth)
        # /sdapi/v1/sd-models, /sdapi/v1/options, /sdapi/v1/samplers - Info endpoints (public)

        path = request.url.path
        is_api_path = path.startswith("/v1/")

        # For SDAPI, only protect image generation endpoints
        is_sdapi_protected = (
            path.startswith("/sdapi/v1/txt2img") or
            path.startswith("/sdapi/v1/img2img")
        )

        if not (is_api_path or is_sdapi_protected):
            return await call_next(request)

        # Extract token (Bearer, Basic, or direct token for AUTOMATIC1111 compatibility)
        auth = request.headers.get("authorization", "")
        token: str = ""

        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
        elif auth.lower().startswith("basic "):
            # AUTOMATIC1111 style: Authorization: Basic <base64_encoded>
            # Decode and extract the token
            import base64
            try:
                decoded = base64.b64decode(auth.split(" ", 1)[1]).decode('utf-8')
                # Format could be "username:password" or just "token" or "token:"
                if ':' in decoded:
                    # Take the first part (username) as the token
                    token = decoded.split(':', 1)[0]
                else:
                    token = decoded
            except Exception:
                pass  # Invalid base64, token remains empty
        elif auth.startswith("pg_"):
            # Direct token: Authorization: pg_xxxx (no prefix)
            token = auth.strip()

        if ENFORCE_KEYS == "1":
            if ADMIN_TOKEN and token == ADMIN_TOKEN:
                ok, why = True, ""
            else:
                ok, why = False, "missing token"
                if token:
                    if USE_REMOTE_VALIDATE == "1" and VALIDATE_URL:
                        try:
                            async with httpx.AsyncClient(timeout=5.0) as client:
                                r = await client.get(
                                    VALIDATE_URL,
                                    headers={"Authorization": f"Bearer {token}"}
                                )
                                ok = (r.status_code == 200)
                                if not ok:
                                    why = f"validator: {r.status_code}"
                        except Exception as e:
                            return JSONResponse(
                                {"error": f"validator error: {e}"},
                                status_code=502
                            )
                    else:
                        ok, why = _token_ok_local(token)
                if not ok:
                    return JSONResponse(
                        {"error": "Unauthorized" if why == "missing token" else why},
                        status_code=(401 if why == "missing token" else (429 if "quota" in why else 401))
                    )

        # Call downstream and measure
        req_bytes = int(request.headers.get("content-length") or 0)
        t0 = time.time()
        response = await call_next(request)
        dur = time.time() - t0

        # Best-effort response size
        try:
            rsp_bytes = int(response.headers.get("content-length") or 0)
        except Exception:
            rsp_bytes = None

        # Optional: map token -> client email for richer usage logs
        client_email: Optional[str] = None
        if token:
            try:
                key_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
                with _db() as c:
                    rr = c.execute(
                        "SELECT client_email FROM api_keys WHERE key_hash=?",
                        (key_hash,)
                    ).fetchone()
                    client_email = rr["client_email"] if rr else None
            except Exception:
                client_email = None

        # Check if endpoint specified per-user metering
        # This allows service key to authenticate but meter to specific user
        metering_token = token
        metering_email: Optional[str] = None

        if hasattr(request.state, "metering_user_email"):
            metering_email = request.state.metering_user_email
            # Look up this user's API key for metering
            try:
                with _db() as c:
                    user_row = c.execute(
                        "SELECT key_prefix FROM api_keys WHERE client_email=? AND is_active=1",
                        (metering_email,)
                    ).fetchone()
                    if user_row:
                        # We only have the prefix, but we can still attribute usage by email
                        # The token field will show the service key, but we log the user email
                        pass
            except Exception:
                pass

        # Pull usage headers if available
        prompt_tokens = _parse_int_header(response.headers.get("X-Usage-Prompt-Tokens"))
        completion_tokens = _parse_int_header(response.headers.get("X-Usage-Completion-Tokens"))

        # Record usage (ignore errors)
        # If metering_email is set, attribute usage to that user instead of service key owner
        usage_email = metering_email if metering_email else client_email
        try:
            with _db() as c:
                c.execute(
                    """INSERT INTO api_usage(token, endpoint, ts, req_bytes, rsp_bytes, prompt_tokens, completion_tokens, user_email)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (token or "", request.url.path, int(t0), req_bytes, rsp_bytes, prompt_tokens, completion_tokens, usage_email)
                )
        except Exception:
            pass

        response.headers["X-Process-Time"] = f"{dur:.3f}s"
        return response
