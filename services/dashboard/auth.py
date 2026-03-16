"""
Authentication module for AI Dashboard.
Handles: bcrypt passwords, JWT sessions, TOTP 2FA, WebAuthn/FIDO2, rate limiting.
"""

import json
import os
import secrets
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import bcrypt
import jwt
import pyotp
import qrcode
import qrcode.image.svg
from io import BytesIO
import base64

# WebAuthn imports
import webauthn
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    UserVerificationRequirement,
    ResidentKeyRequirement,
)
from webauthn.helpers.cose import COSEAlgorithmIdentifier

DATA_DIR = Path("/opt/ai/dashboard/data")
USERS_FILE = DATA_DIR / "users.json"
WEBAUTHN_FILE = DATA_DIR / "webauthn_creds.json"
JWT_KEY_FILE = DATA_DIR / "jwt_secret.key"
CHALLENGES_FILE = DATA_DIR / "webauthn_challenges.json"

RP_ID = "dash.YOURDOMAIN.COM"
RP_NAME = "AI System Dashboard"
RP_ORIGIN = "https://dash.YOURDOMAIN.COM"

# In-memory rate limiter: {ip: [(timestamp, count)]}
_rate_limit_store: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT_MAX = 10
RATE_LIMIT_WINDOW = 15 * 60  # 15 minutes


# ---------------------------------------------------------------------------
# JWT secret
# ---------------------------------------------------------------------------

def _get_jwt_secret() -> str:
    JWT_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if JWT_KEY_FILE.exists():
        return JWT_KEY_FILE.read_text().strip()
    secret = secrets.token_hex(32)
    JWT_KEY_FILE.write_text(secret)
    JWT_KEY_FILE.chmod(0o600)
    return secret


JWT_SECRET = None  # Lazy-loaded


def get_jwt_secret() -> str:
    global JWT_SECRET
    if JWT_SECRET is None:
        JWT_SECRET = _get_jwt_secret()
    return JWT_SECRET


# ---------------------------------------------------------------------------
# User DB
# ---------------------------------------------------------------------------

class UserDB:
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        if not USERS_FILE.exists():
            return {}
        try:
            return json.loads(USERS_FILE.read_text())
        except Exception:
            return {}

    def _save(self, data: dict):
        USERS_FILE.write_text(json.dumps(data, indent=2))
        USERS_FILE.chmod(0o600)

    def has_users(self) -> bool:
        data = self._load()
        return bool(data)

    def get_user(self, email: str) -> Optional[dict]:
        data = self._load()
        return data.get(email.lower())

    def create_user(self, email: str, password_hash: str, totp_secret: str) -> dict:
        data = self._load()
        user = {
            "email": email.lower(),
            "password_hash": password_hash,
            "totp_secret": totp_secret,
            "totp_verified": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "id": secrets.token_hex(16),
        }
        data[email.lower()] = user
        self._save(data)
        return user

    def update_user(self, email: str, updates: dict):
        data = self._load()
        if email.lower() in data:
            data[email.lower()].update(updates)
            self._save(data)

    def mark_totp_verified(self, email: str):
        self.update_user(email, {"totp_verified": True})


user_db = UserDB()


# ---------------------------------------------------------------------------
# Password
# ---------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# JWT sessions
# ---------------------------------------------------------------------------

def create_session(user_id: str, email: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "iat": int(time.time()),
        "exp": int(time.time()) + 8 * 3600,  # 8 hours
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm="HS256")


def verify_session(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def check_rate_limit(ip: str) -> bool:
    """Return True if request is allowed, False if rate limited."""
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW
    attempts = _rate_limit_store[ip]
    # Remove old entries
    attempts = [t for t in attempts if t > window_start]
    _rate_limit_store[ip] = attempts
    if len(attempts) >= RATE_LIMIT_MAX:
        return False
    attempts.append(now)
    _rate_limit_store[ip] = attempts
    return True


def record_failed_attempt(ip: str):
    """Record a failed login attempt (already done in check_rate_limit, but explicit)."""
    pass  # check_rate_limit already records


# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------

def generate_totp_secret() -> str:
    return pyotp.random_base32()


def get_totp_uri(secret: str, email: str) -> str:
    return pyotp.totp.TOTP(secret).provisioning_uri(
        name=email,
        issuer_name="AI Dashboard"
    )


def verify_totp(secret: str, code: str) -> bool:
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=1)


def generate_totp_qr_base64(secret: str, email: str) -> str:
    """Generate QR code as base64 PNG string."""
    uri = get_totp_uri(secret, email)
    qr = qrcode.QRCode(version=1, box_size=6, border=4)
    qr.add_data(uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------------------
# WebAuthn
# ---------------------------------------------------------------------------

def _load_webauthn_creds() -> dict:
    if not WEBAUTHN_FILE.exists():
        return {}
    try:
        return json.loads(WEBAUTHN_FILE.read_text())
    except Exception:
        return {}


def _save_webauthn_creds(data: dict):
    WEBAUTHN_FILE.write_text(json.dumps(data, indent=2))
    WEBAUTHN_FILE.chmod(0o600)


def _load_challenges() -> dict:
    if not CHALLENGES_FILE.exists():
        return {}
    try:
        return json.loads(CHALLENGES_FILE.read_text())
    except Exception:
        return {}


def _save_challenges(data: dict):
    CHALLENGES_FILE.write_text(json.dumps(data, indent=2))
    CHALLENGES_FILE.chmod(0o600)


def _store_challenge(key: str, challenge_b64: str, extra: dict = None):
    challenges = _load_challenges()
    challenges[key] = {
        "challenge": challenge_b64,
        "created_at": time.time(),
        **(extra or {}),
    }
    _save_challenges(challenges)


def _pop_challenge(key: str) -> Optional[dict]:
    challenges = _load_challenges()
    entry = challenges.pop(key, None)
    _save_challenges(challenges)
    if entry and time.time() - entry.get("created_at", 0) > 300:
        return None  # expired (5 min)
    return entry


def webauthn_begin_registration(email: str, user_id: str) -> dict:
    """Begin WebAuthn registration ceremony."""
    creds_data = _load_webauthn_creds()
    user_creds = creds_data.get(email, [])

    # Build exclude_credentials from existing creds
    exclude_credentials = []
    for cred in user_creds:
        exclude_credentials.append(
            webauthn.helpers.structs.PublicKeyCredentialDescriptor(
                id=base64.b64decode(cred["id"])
            )
        )

    options = webauthn.generate_registration_options(
        rp_id=RP_ID,
        rp_name=RP_NAME,
        user_id=user_id.encode(),
        user_name=email,
        user_display_name=email,
        exclude_credentials=exclude_credentials,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        supported_pub_key_algs=[
            COSEAlgorithmIdentifier.ECDSA_SHA_256,
            COSEAlgorithmIdentifier.RSASSA_PKCS1_v1_5_SHA_256,
        ],
    )

    challenge_b64 = base64.b64encode(options.challenge).decode()
    _store_challenge(f"reg:{email}", challenge_b64)

    return webauthn.options_to_json(options)


def webauthn_complete_registration(email: str, credential_json: str, credential_name: str = "Key") -> dict:
    """Complete WebAuthn registration and store credential."""
    challenge_entry = _pop_challenge(f"reg:{email}")
    if not challenge_entry:
        raise ValueError("Registration challenge expired or not found")

    challenge_bytes = base64.b64decode(challenge_entry["challenge"])

    credential = webauthn.helpers.parse_registration_credential_json(credential_json)

    verification = webauthn.verify_registration_response(
        credential=credential,
        expected_challenge=challenge_bytes,
        expected_rp_id=RP_ID,
        expected_origin=RP_ORIGIN,
    )

    creds_data = _load_webauthn_creds()
    user_creds = creds_data.get(email, [])

    new_cred = {
        "id": base64.b64encode(verification.credential_id).decode(),
        "public_key": base64.b64encode(verification.credential_public_key).decode(),
        "sign_count": verification.sign_count,
        "name": credential_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "aaguid": verification.aaguid if verification.aaguid else None,
    }
    user_creds.append(new_cred)
    creds_data[email] = user_creds
    _save_webauthn_creds(creds_data)

    return {"success": True, "credential_id": new_cred["id"], "name": credential_name}


def webauthn_begin_authentication(email: str) -> Optional[dict]:
    """Begin WebAuthn authentication ceremony."""
    creds_data = _load_webauthn_creds()
    user_creds = creds_data.get(email, [])
    if not user_creds:
        return None

    allow_credentials = []
    for cred in user_creds:
        allow_credentials.append(
            webauthn.helpers.structs.PublicKeyCredentialDescriptor(
                id=base64.b64decode(cred["id"])
            )
        )

    options = webauthn.generate_authentication_options(
        rp_id=RP_ID,
        allow_credentials=allow_credentials,
        user_verification=UserVerificationRequirement.PREFERRED,
    )

    challenge_b64 = base64.b64encode(options.challenge).decode()
    _store_challenge(f"auth:{email}", challenge_b64)

    return webauthn.options_to_json(options)


def webauthn_complete_authentication(email: str, credential_json: str) -> bool:
    """Complete WebAuthn authentication and update sign count."""
    challenge_entry = _pop_challenge(f"auth:{email}")
    if not challenge_entry:
        return False

    challenge_bytes = base64.b64decode(challenge_entry["challenge"])

    credential = webauthn.helpers.parse_authentication_credential_json(credential_json)

    creds_data = _load_webauthn_creds()
    user_creds = creds_data.get(email, [])

    # Find matching credential
    # credential.raw_id may be bytes already in v2.7
    raw_id = credential.raw_id if isinstance(credential.raw_id, bytes) else bytes(credential.raw_id)
    cred_id_b64 = base64.b64encode(raw_id).decode()
    matching = None
    for cred in user_creds:
        if cred["id"] == cred_id_b64:
            matching = cred
            break

    if not matching:
        return False

    try:
        verification = webauthn.verify_authentication_response(
            credential=credential,
            expected_challenge=challenge_bytes,
            expected_rp_id=RP_ID,
            expected_origin=RP_ORIGIN,
            credential_public_key=base64.b64decode(matching["public_key"]),
            credential_current_sign_count=matching["sign_count"],
        )
        # Update sign count
        matching["sign_count"] = verification.new_sign_count
        _save_webauthn_creds(creds_data)
        return True
    except Exception:
        return False


def get_webauthn_credentials(email: str) -> list[dict]:
    """Return list of registered WebAuthn credentials for user."""
    creds_data = _load_webauthn_creds()
    creds = creds_data.get(email, [])
    return [{"id": c["id"], "name": c["name"], "created_at": c["created_at"]} for c in creds]


def delete_webauthn_credential(email: str, cred_id: str) -> bool:
    creds_data = _load_webauthn_creds()
    user_creds = creds_data.get(email, [])
    new_creds = [c for c in user_creds if c["id"] != cred_id]
    if len(new_creds) == len(user_creds):
        return False
    creds_data[email] = new_creds
    _save_webauthn_creds(creds_data)
    return True


def has_webauthn_credentials(email: str) -> bool:
    creds_data = _load_webauthn_creds()
    return bool(creds_data.get(email))
