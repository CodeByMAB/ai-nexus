"""
Security Utilities
Input validation, sanitization, and security helpers
"""
import re
import html
import hashlib
import secrets
from typing import Optional, Any, Dict, List
from datetime import datetime, timedelta
import structlog

logger = structlog.get_logger()

# ========================================
# Input Validation
# ========================================

# Whitelists for strict validation
ALLOWED_PLAN_TIERS = {'trial', 'family', 'regular', 'ultra_privacy', 'beta'}
ALLOWED_PACK_TYPES = {'trial', 'small', 'medium', 'large'}
ALLOWED_PAYMENT_METHODS = {'stripe', 'strike_usd', 'strike_btc', 'btcpay'}
ALLOWED_PAYMENT_TYPES = {'subscription', 'token_pack'}
ALLOWED_CURRENCIES = {'USD', 'BTC', 'EUR', 'GBP'}
ALLOWED_SUBSCRIPTION_STATUSES = {'active', 'trialing', 'past_due', 'canceled', 'unpaid'}

# Email validation regex (RFC 5322 simplified)
EMAIL_REGEX = re.compile(
    r'^[a-zA-Z0-9.!#$%&\'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}'
    r'[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$'
)

# Max lengths for various fields
MAX_EMAIL_LENGTH = 254
MAX_NAME_LENGTH = 100
MAX_NOTES_LENGTH = 500
MAX_DESCRIPTION_LENGTH = 200
MAX_PROMO_CODE_LENGTH = 50
MAX_SUBSCRIPTION_ID_LENGTH = 255
MAX_PAYMENT_ID_LENGTH = 255


def validate_email(email: str) -> tuple[bool, Optional[str]]:
    """
    Validate email address format and length

    Returns:
        (valid: bool, error_message: str | None)
    """
    if not email:
        return False, "Email is required"

    if len(email) > MAX_EMAIL_LENGTH:
        return False, f"Email too long (max {MAX_EMAIL_LENGTH} characters)"

    if not EMAIL_REGEX.match(email):
        return False, "Invalid email format"

    # Check for common injection patterns
    dangerous_chars = ['\n', '\r', '\0', '<', '>', '"']
    if any(char in email for char in dangerous_chars):
        return False, "Email contains invalid characters"

    return True, None


def validate_plan_tier(plan_tier: str) -> tuple[bool, Optional[str]]:
    """Validate plan tier against whitelist"""
    if not plan_tier:
        return False, "Plan tier is required"

    if plan_tier not in ALLOWED_PLAN_TIERS:
        return False, f"Invalid plan tier. Allowed: {', '.join(ALLOWED_PLAN_TIERS)}"

    return True, None


def validate_pack_type(pack_type: str) -> tuple[bool, Optional[str]]:
    """Validate pack type against whitelist"""
    if not pack_type:
        return False, "Pack type is required"

    if pack_type not in ALLOWED_PACK_TYPES:
        return False, f"Invalid pack type. Allowed: {', '.join(ALLOWED_PACK_TYPES)}"

    return True, None


def validate_payment_method(payment_method: str) -> tuple[bool, Optional[str]]:
    """Validate payment method against whitelist"""
    if not payment_method:
        return False, "Payment method is required"

    if payment_method not in ALLOWED_PAYMENT_METHODS:
        return False, f"Invalid payment method. Allowed: {', '.join(ALLOWED_PAYMENT_METHODS)}"

    return True, None


def validate_currency(currency: str) -> tuple[bool, Optional[str]]:
    """Validate currency code"""
    if not currency:
        return False, "Currency is required"

    currency = currency.upper()
    if currency not in ALLOWED_CURRENCIES:
        return False, f"Invalid currency. Allowed: {', '.join(ALLOWED_CURRENCIES)}"

    return True, None


def validate_amount(amount: float, min_amount: float = 0.01, max_amount: float = 10000.0) -> tuple[bool, Optional[str]]:
    """
    Validate payment amount

    Args:
        amount: Amount to validate
        min_amount: Minimum allowed amount
        max_amount: Maximum allowed amount
    """
    if not isinstance(amount, (int, float)):
        return False, "Amount must be a number"

    if amount < min_amount:
        return False, f"Amount too small (minimum ${min_amount})"

    if amount > max_amount:
        return False, f"Amount too large (maximum ${max_amount})"

    # Check for suspicious patterns
    if amount < 0:
        return False, "Amount cannot be negative"

    return True, None


def validate_string_length(value: str, field_name: str, max_length: int) -> tuple[bool, Optional[str]]:
    """Validate string length"""
    if not value:
        return True, None  # Empty strings are handled by required checks

    if len(value) > max_length:
        return False, f"{field_name} too long (max {max_length} characters)"

    return True, None


def validate_promo_code(code: str) -> tuple[bool, Optional[str]]:
    """Validate promo code format"""
    if not code:
        return False, "Promo code is required"

    # Check length
    if len(code) > MAX_PROMO_CODE_LENGTH:
        return False, f"Promo code too long (max {MAX_PROMO_CODE_LENGTH} characters)"

    # Only allow alphanumeric and underscores
    if not re.match(r'^[A-Z0-9_]+$', code.upper()):
        return False, "Promo code can only contain letters, numbers, and underscores"

    return True, None


# ========================================
# Input Sanitization
# ========================================

def sanitize_email(email: str) -> str:
    """Sanitize email address"""
    if not email:
        return ""

    # Remove whitespace and convert to lowercase
    email = email.strip().lower()

    # Remove any null bytes or control characters
    email = ''.join(char for char in email if ord(char) >= 32)

    return email[:MAX_EMAIL_LENGTH]


def sanitize_string(value: str, max_length: int = 255) -> str:
    """
    Sanitize general string input
    Removes control characters and limits length
    """
    if not value:
        return ""

    # Remove null bytes and control characters (except newlines for notes)
    value = ''.join(char for char in value if ord(char) >= 32 or char in '\n\r')

    # Trim whitespace
    value = value.strip()

    # Limit length
    return value[:max_length]


def sanitize_html(text: str) -> str:
    """
    Escape HTML entities to prevent XSS
    Use this for any user input displayed in HTML
    """
    if not text:
        return ""

    return html.escape(text, quote=True)


def sanitize_sql_identifier(identifier: str, allowed_values: set) -> Optional[str]:
    """
    Sanitize SQL identifiers (table names, column names)
    Only allows values from a whitelist

    Args:
        identifier: The identifier to sanitize
        allowed_values: Set of allowed values

    Returns:
        The identifier if valid, None otherwise
    """
    if identifier in allowed_values:
        return identifier

    logger.warning("sql_identifier_rejected", identifier=identifier)
    return None


def redact_sensitive_data(data: Dict[str, Any], fields_to_redact: List[str]) -> Dict[str, Any]:
    """
    Redact sensitive fields from a dictionary
    Used for safe logging

    Args:
        data: Dictionary to redact
        fields_to_redact: List of field names to redact

    Returns:
        Dictionary with sensitive fields redacted
    """
    redacted = data.copy()

    for field in fields_to_redact:
        if field in redacted:
            value = redacted[field]
            if isinstance(value, str) and len(value) > 8:
                # Show first 4 characters, redact rest
                redacted[field] = f"{value[:4]}...REDACTED"
            else:
                redacted[field] = "REDACTED"

    return redacted


# ========================================
# Webhook Security
# ========================================

def verify_webhook_timestamp(timestamp: int, max_age_seconds: int = 300) -> bool:
    """
    Verify webhook timestamp to prevent replay attacks

    Args:
        timestamp: Unix timestamp from webhook
        max_age_seconds: Maximum allowed age (default 5 minutes)

    Returns:
        True if timestamp is valid and recent
    """
    current_time = int(datetime.now().timestamp())

    # Check if timestamp is in the past
    if timestamp > current_time:
        logger.warning("webhook_timestamp_future", timestamp=timestamp)
        return False

    # Check if timestamp is too old
    age = current_time - timestamp
    if age > max_age_seconds:
        logger.warning("webhook_timestamp_too_old", age=age, max_age=max_age_seconds)
        return False

    return True


def generate_secure_token(length: int = 32) -> str:
    """Generate a cryptographically secure random token"""
    return secrets.token_urlsafe(length)


def hash_event_id(event_id: str) -> str:
    """Hash event ID for deduplication tracking"""
    return hashlib.sha256(event_id.encode('utf-8')).hexdigest()


# ========================================
# PGP Input Validation
# ========================================

def validate_pgp_public_key(key: str) -> tuple[bool, Optional[str]]:
    """
    Validate PGP public key format

    Returns:
        (valid: bool, error_message: str | None)
    """
    if not key:
        return False, "PGP key is required"

    # Check for PGP header/footer
    if not key.strip().startswith('-----BEGIN PGP PUBLIC KEY BLOCK-----'):
        return False, "Invalid PGP key format (missing header)"

    if not key.strip().endswith('-----END PGP PUBLIC KEY BLOCK-----'):
        return False, "Invalid PGP key format (missing footer)"

    # Check length (reasonable limits)
    if len(key) < 500:
        return False, "PGP key too short (likely invalid)"

    if len(key) > 100000:
        return False, "PGP key too large"

    # Check for dangerous characters that could indicate command injection
    dangerous_patterns = [';', '&&', '||', '`', '$(',  '${']
    if any(pattern in key for pattern in dangerous_patterns):
        return False, "PGP key contains invalid characters"

    return True, None


def sanitize_pgp_fingerprint(fingerprint: str) -> str:
    """Sanitize PGP fingerprint to only hexadecimal characters"""
    if not fingerprint:
        return ""

    # Remove spaces and convert to uppercase
    fingerprint = fingerprint.replace(' ', '').upper()

    # Only keep hex characters
    fingerprint = ''.join(c for c in fingerprint if c in '0123456789ABCDEF')

    return fingerprint


# ========================================
# API Key Validation
# ========================================

def validate_api_key_format(api_key: str) -> bool:
    """Validate API key format (pg_xxxx)"""
    if not api_key:
        return False

    if not api_key.startswith('pg_'):
        return False

    if len(api_key) < 20:  # Minimum reasonable length
        return False

    if len(api_key) > 100:  # Maximum to prevent abuse
        return False

    # Only alphanumeric and underscores after prefix
    if not re.match(r'^pg_[A-Za-z0-9_-]+$', api_key):
        return False

    return True


# ========================================
# Business Logic Validation
# ========================================

def validate_token_amount(tokens: int, min_tokens: int = 1, max_tokens: int = 10000000) -> tuple[bool, Optional[str]]:
    """Validate token amounts"""
    if not isinstance(tokens, int):
        return False, "Token amount must be an integer"

    if tokens < min_tokens:
        return False, f"Token amount too small (minimum {min_tokens})"

    if tokens > max_tokens:
        return False, f"Token amount too large (maximum {max_tokens})"

    return True, None


def validate_subscription_id(subscription_id: str) -> tuple[bool, Optional[str]]:
    """Validate subscription ID format"""
    if not subscription_id:
        return False, "Subscription ID is required"

    if len(subscription_id) > MAX_SUBSCRIPTION_ID_LENGTH:
        return False, f"Subscription ID too long (max {MAX_SUBSCRIPTION_ID_LENGTH})"

    # Check for suspicious characters
    if any(char in subscription_id for char in ['\n', '\r', '\0', '<', '>', '"', "'"]):
        return False, "Subscription ID contains invalid characters"

    return True, None


# ========================================
# Path Validation
# ========================================

def validate_file_path(path: str, allowed_base_paths: List[str]) -> tuple[bool, Optional[str]]:
    """
    Validate file path to prevent directory traversal

    Args:
        path: Path to validate
        allowed_base_paths: List of allowed base directories

    Returns:
        (valid: bool, error_message: str | None)
    """
    import os

    # Resolve to absolute path
    abs_path = os.path.abspath(path)

    # Check if path is within allowed directories
    for base_path in allowed_base_paths:
        abs_base = os.path.abspath(base_path)
        if abs_path.startswith(abs_base):
            return True, None

    return False, "Path not in allowed directories"
