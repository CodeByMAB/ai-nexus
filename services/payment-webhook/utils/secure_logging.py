"""
Secure Logging Utilities
Automatic credential redaction for structured logs
"""
import re
from typing import Any, Dict
import structlog


# Sensitive field patterns
SENSITIVE_FIELDS = {
    # API Keys and tokens
    'api_key', 'apikey', 'token', 'bearer', 'authorization',
    'api_secret', 'secret_key', 'access_key', 'private_key',

    # Payment information
    'card_number', 'cvv', 'card_cvv', 'card_cvc',
    'stripe_secret_key', 'stripe_webhook_secret',
    'strike_api_key', 'strike_webhook_secret',
    'btcpay_api_key',

    # Credentials
    'password', 'passwd', 'pwd', 'secret',
    'sendgrid_api_key', 'smtp_password',

    # PGP keys (full keys, not fingerprints)
    'pgp_public_key', 'pgp_private_key', 'public_key', 'private_key',

    # Admin keys
    'admin_api_keys',
}

# Patterns that indicate sensitive data in values (regex)
SENSITIVE_VALUE_PATTERNS = [
    # API key patterns
    r'pg_[A-Za-z0-9_-]{20,}',  # Our API keys
    r'sk_live_[A-Za-z0-9]{24,}',  # Stripe live secret keys
    r'sk_test_[A-Za-z0-9]{24,}',  # Stripe test secret keys
    r'whsec_[A-Za-z0-9]{32,}',  # Stripe webhook secrets
    r'pk_live_[A-Za-z0-9]{24,}',  # Stripe publishable keys
    r'pk_test_[A-Za-z0-9]{24,}',  # Stripe publishable keys
    r'SG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}',  # SendGrid API keys
    r'-----BEGIN (RSA |PGP |)PRIVATE KEY-----',  # Private keys
    r'-----BEGIN PGP PUBLIC KEY BLOCK-----[\s\S]*-----END PGP PUBLIC KEY BLOCK-----',  # Full PGP keys
]


def redact_api_key(value: str) -> str:
    """
    Redact API key showing only prefix

    Args:
        value: API key string

    Returns:
        Redacted API key (e.g., "pg_xxxx...****")
    """
    if not value or len(value) < 8:
        return "****"

    # Show prefix to identify key type
    if value.startswith('pg_'):
        return f"{value[:7]}...****"
    elif value.startswith('sk_'):
        return f"{value[:10]}...****"
    elif value.startswith('pk_'):
        return f"{value[:10]}...****"
    elif value.startswith('whsec_'):
        return f"whsec_****"
    elif value.startswith('SG.'):
        return f"SG.****"
    else:
        return f"{value[:4]}...****"


def redact_email(email: str) -> str:
    """
    Partially redact email address

    Args:
        email: Email address

    Returns:
        Partially redacted email (e.g., "u***@example.com")
    """
    if not email or '@' not in email:
        return "***@***.***"

    local, domain = email.split('@', 1)

    if len(local) <= 2:
        redacted_local = "*" * len(local)
    else:
        # Show first character, redact middle, show nothing from end
        redacted_local = local[0] + "*" * (len(local) - 1)

    return f"{redacted_local}@{domain}"


def redact_fingerprint(fingerprint: str) -> str:
    """
    Partially redact PGP fingerprint

    Args:
        fingerprint: PGP fingerprint

    Returns:
        Partially redacted fingerprint
    """
    if not fingerprint or len(fingerprint) < 16:
        return "****"

    return f"{fingerprint[:16]}...****"


def redact_value(value: Any) -> Any:
    """
    Redact sensitive values based on content

    Args:
        value: Value to check and potentially redact

    Returns:
        Original or redacted value
    """
    if not isinstance(value, str):
        return value

    # Check against sensitive patterns
    for pattern in SENSITIVE_VALUE_PATTERNS:
        if re.search(pattern, value):
            # If it's a full PGP key, show it's a key
            if 'BEGIN' in value and 'KEY' in value:
                return "[PGP KEY REDACTED]"
            # If it's an API key pattern, redact it
            return redact_api_key(value)

    return value


def redact_sensitive_fields(event_dict: Dict[str, Any]) -> Dict[str, Any]:
    """
    Redact sensitive fields from log event dictionary

    Args:
        event_dict: Log event dictionary

    Returns:
        Event dictionary with sensitive fields redacted
    """
    redacted = event_dict.copy()

    for key, value in event_dict.items():
        key_lower = key.lower()

        # Check if field name is sensitive
        if any(sensitive in key_lower for sensitive in SENSITIVE_FIELDS):
            if isinstance(value, str):
                # Redact based on content
                if 'key' in key_lower and len(value) > 10:
                    redacted[key] = redact_api_key(value)
                elif 'email' in key_lower:
                    redacted[key] = redact_email(value)
                elif 'password' in key_lower or 'secret' in key_lower:
                    redacted[key] = "****REDACTED****"
                elif 'fingerprint' in key_lower:
                    redacted[key] = redact_fingerprint(value)
                else:
                    redacted[key] = "****REDACTED****"
            else:
                redacted[key] = "****REDACTED****"

        # Check if value contains sensitive patterns
        elif isinstance(value, str):
            redacted_value = redact_value(value)
            if redacted_value != value:
                redacted[key] = redacted_value

        # Handle nested dictionaries
        elif isinstance(value, dict):
            redacted[key] = redact_sensitive_fields(value)

    return redacted


class SecureLogProcessor:
    """
    Structlog processor for automatic credential redaction

    Usage:
        Add to structlog processors:
        structlog.configure(
            processors=[
                ...
                SecureLogProcessor(),
                ...
            ]
        )
    """

    def __call__(self, logger, method_name, event_dict):
        """
        Process log event and redact sensitive data

        Args:
            logger: Logger instance
            method_name: Log method name (info, error, etc.)
            event_dict: Event dictionary

        Returns:
            Redacted event dictionary
        """
        return redact_sensitive_fields(event_dict)


def mask_card_number(card_number: str) -> str:
    """
    Mask credit card number showing only last 4 digits

    Args:
        card_number: Credit card number

    Returns:
        Masked card number (e.g., "****1234")
    """
    if not card_number:
        return "****"

    # Remove spaces and dashes
    cleaned = card_number.replace(' ', '').replace('-', '')

    if len(cleaned) < 4:
        return "****"

    return f"****{cleaned[-4:]}"


def safe_log_payment_info(
    amount: float,
    currency: str,
    payment_method: str,
    customer_email: str,
    **kwargs
) -> Dict[str, Any]:
    """
    Create a safe log entry for payment information

    Args:
        amount: Payment amount
        currency: Currency code
        payment_method: Payment method
        customer_email: Customer email
        **kwargs: Additional fields

    Returns:
        Dictionary safe for logging
    """
    log_data = {
        "amount": amount,
        "currency": currency,
        "payment_method": payment_method,
        "customer_email": redact_email(customer_email),
    }

    # Redact any additional sensitive fields
    for key, value in kwargs.items():
        if key.lower() in SENSITIVE_FIELDS:
            log_data[key] = "****REDACTED****"
        elif isinstance(value, str):
            log_data[key] = redact_value(value)
        else:
            log_data[key] = value

    return log_data


# Example usage in application code:
def log_payment_event_safely(logger, event_type: str, customer_email: str, amount: float, **kwargs):
    """
    Helper function to log payment events with automatic redaction

    Example:
        log_payment_event_safely(
            logger,
            "payment_successful",
            customer_email="user@example.com",
            amount=25.00,
            api_key="pg_secret123",  # Will be redacted
            stripe_key="sk_live_xxx"  # Will be redacted
        )
    """
    log_data = safe_log_payment_info(amount, kwargs.get('currency', 'USD'),
                                      kwargs.get('payment_method', 'unknown'),
                                      customer_email, **kwargs)
    logger.info(event_type, **log_data)
