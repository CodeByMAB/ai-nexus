"""
PGP Handler Service
Handles PGP encryption for ultra-privacy tier users
"""
import gnupg
import os
from typing import Optional
import structlog

from config import settings
from utils.security import validate_pgp_public_key, sanitize_pgp_fingerprint, sanitize_string

logger = structlog.get_logger()

# Initialize GPG
def get_gpg():
    """Get GPG instance with configured home directory"""
    # Ensure GPG home directory exists
    os.makedirs(settings.gpg_home, exist_ok=True)
    os.chmod(settings.gpg_home, 0o700)  # Secure permissions

    return gnupg.GPG(gnupghome=settings.gpg_home)


async def import_public_key(public_key: str) -> Optional[str]:
    """
    Import a user's PGP public key

    Args:
        public_key: ASCII-armored PGP public key

    Returns:
        Key fingerprint if successful, None otherwise
    """
    if not settings.gpg_enable_encryption:
        logger.warning("pgp_encryption_disabled")
        return None

    # Validate PGP key format before importing
    valid, error = validate_pgp_public_key(public_key)
    if not valid:
        logger.error("pgp_key_validation_failed", error=error)
        return None

    # Sanitize: ensure it's a string and limit length
    public_key = sanitize_string(public_key, max_length=100000)

    try:
        gpg = get_gpg()
        import_result = gpg.import_keys(public_key)

        if import_result.count > 0:
            fingerprint = import_result.fingerprints[0]

            # Sanitize fingerprint
            fingerprint = sanitize_pgp_fingerprint(fingerprint)

            logger.info(
                "pgp_key_imported",
                fingerprint=fingerprint[:16] + "...",  # Partial fingerprint for logging
                count=import_result.count
            )
            return fingerprint
        else:
            logger.error(
                "pgp_key_import_failed",
                result=import_result.results
            )
            return None

    except Exception as e:
        logger.error("pgp_import_exception", error=str(e))
        return None


async def encrypt_message(message: str, recipient_key: str) -> Optional[str]:
    """
    Encrypt a message with a recipient's public key

    Args:
        message: Plain text message to encrypt
        recipient_key: ASCII-armored public key or fingerprint

    Returns:
        ASCII-armored encrypted message, or None if encryption fails
    """
    if not settings.gpg_enable_encryption:
        logger.warning("pgp_encryption_disabled", returning_plaintext=False)
        return None

    # Validate and sanitize inputs
    if not message or not recipient_key:
        logger.error("pgp_encrypt_missing_params")
        return None

    # Sanitize message (limit size to prevent DoS)
    message = sanitize_string(message, max_length=1000000)  # 1MB limit

    try:
        gpg = get_gpg()

        # If recipient_key looks like a full public key, import it first
        if 'BEGIN PGP PUBLIC KEY BLOCK' in recipient_key:
            # Validate key format
            valid, error = validate_pgp_public_key(recipient_key)
            if not valid:
                logger.error("pgp_invalid_recipient_key", error=error)
                return None

            fingerprint = await import_public_key(recipient_key)
            if not fingerprint:
                logger.error("pgp_encryption_failed_import")
                return None
            recipient = fingerprint
        else:
            # Assume it's already a fingerprint - sanitize it
            recipient = sanitize_pgp_fingerprint(recipient_key)
            if not recipient or len(recipient) < 16:
                logger.error("pgp_invalid_fingerprint", fingerprint=recipient_key[:20])
                return None

        # Encrypt the message
        encrypted_data = gpg.encrypt(message, recipient, always_trust=True)

        if encrypted_data.ok:
            encrypted_message = str(encrypted_data)
            logger.info(
                "message_encrypted",
                recipient=recipient[:16] + "...",
                size=len(encrypted_message)
            )
            return encrypted_message
        else:
            logger.error(
                "pgp_encryption_failed",
                status=encrypted_data.status,
                stderr=str(encrypted_data.stderr)[:200]  # Limit stderr logging
            )
            return None

    except Exception as e:
        logger.error("pgp_encrypt_exception", error=str(e)[:200])
        return None


async def verify_public_key(public_key: str) -> bool:
    """
    Verify that a public key is valid

    Args:
        public_key: ASCII-armored PGP public key

    Returns:
        True if valid, False otherwise
    """
    # Validate format first
    valid, error = validate_pgp_public_key(public_key)
    if not valid:
        logger.warning("pgp_verify_invalid_format", error=error)
        return False

    try:
        gpg = get_gpg()

        # Sanitize key
        public_key = sanitize_string(public_key, max_length=100000)

        import_result = gpg.import_keys(public_key)

        return import_result.count > 0

    except Exception as e:
        logger.error("pgp_verify_exception", error=str(e)[:200])
        return False


async def get_key_info(fingerprint: str) -> Optional[dict]:
    """
    Get information about an imported key

    Args:
        fingerprint: Key fingerprint

    Returns:
        Dictionary with key information or None
    """
    # Sanitize fingerprint
    fingerprint = sanitize_pgp_fingerprint(fingerprint)
    if not fingerprint or len(fingerprint) < 16:
        logger.error("pgp_invalid_fingerprint_for_info")
        return None

    try:
        gpg = get_gpg()
        keys = gpg.list_keys()

        for key in keys:
            if key['fingerprint'] == fingerprint:
                return {
                    "fingerprint": key['fingerprint'],
                    "keyid": key['keyid'],
                    "uids": key['uids'],
                    "length": key['length'],
                    "date": key['date'],
                    "expires": key.get('expires', 'never')
                }

        return None

    except Exception as e:
        logger.error("pgp_key_info_exception", error=str(e)[:200])
        return None


async def encrypt_api_key_for_user(
    api_key: str,
    user_email: str,
    pgp_public_key: str,
    plan_tier: str,
    monthly_price: float
) -> Optional[str]:
    """
    Encrypt an API key along with usage instructions for a user

    This creates a complete encrypted message with the API key and instructions

    Args:
        api_key: The API key to encrypt
        user_email: User's email
        pgp_public_key: User's PGP public key
        plan_tier: Subscription plan tier
        monthly_price: Monthly subscription price

    Returns:
        Encrypted message or None
    """
    # Validate and sanitize inputs
    if not api_key or not user_email or not pgp_public_key or not plan_tier:
        logger.error("pgp_encrypt_api_key_missing_params")
        return None

    # Sanitize inputs (prevent injection into the message template)
    api_key_safe = sanitize_string(api_key, max_length=200)
    plan_tier_safe = sanitize_string(plan_tier, max_length=50)

    # Validate PGP key before proceeding
    valid, error = validate_pgp_public_key(pgp_public_key)
    if not valid:
        logger.error("pgp_invalid_public_key_for_user", error=error)
        return None

    # Build the message
    message = f"""
========================================
PLAYGROUND AI - API KEY
========================================

Welcome to Playground AI!

Your {plan_tier_safe.replace('_', ' ').title()} subscription is active.

API KEY:
{api_key_safe}

⚠️  IMPORTANT: Keep this key secure! It will not be shown again.

========================================
PLAN DETAILS
========================================

Plan: {plan_tier_safe.replace('_', ' ').title()}
Price: ${monthly_price:.2f}/month
Monthly Tokens: 500,000

========================================
GETTING STARTED
========================================

API Base URL:
https://api.YOURDOMAIN.COM/v1

Documentation:
{settings.api_docs_url}

Example Usage (Python):
```python
from openai import OpenAI

client = OpenAI(
    api_key="{api_key_safe}",
    base_url="https://api.YOURDOMAIN.COM/v1"
)

response = client.chat.completions.create(
    model="gpt-oss:20b",
    messages=[{{"role": "user", "content": "Hello!"}}]
)

print(response.choices[0].message.content)
```

========================================
SUPPORT
========================================

Email: {settings.support_email}
Website: {settings.signup_website_url}

Thank you for choosing Playground AI!

This message was encrypted with your PGP public key
for maximum privacy and security.
========================================
"""

    # Encrypt the message (encrypt_message will validate again)
    return await encrypt_message(message, pgp_public_key)
