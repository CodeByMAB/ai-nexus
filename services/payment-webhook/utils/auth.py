"""
Authentication Utilities
API key authentication for public endpoints
"""
import secrets
from typing import Optional
from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader, HTTPBearer, HTTPAuthorizationCredentials
from fastapi.security.api_key import APIKeyHeader
import structlog

from config import settings

logger = structlog.get_logger()

# API Key header schemes
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
bearer_header = HTTPBearer(auto_error=False)


def constant_time_compare(a: str, b: str) -> bool:
    """
    Constant-time string comparison to prevent timing attacks

    Args:
        a: First string
        b: Second string

    Returns:
        True if strings match
    """
    return secrets.compare_digest(a.encode('utf-8'), b.encode('utf-8'))


async def verify_api_key(
    api_key: Optional[str] = Security(api_key_header),
    bearer: Optional[HTTPAuthorizationCredentials] = Security(bearer_header)
) -> str:
    """
    Verify API key from X-API-Key header or Authorization: Bearer token

    Args:
        api_key: API key from X-API-Key header
        bearer: Bearer token from Authorization header

    Returns:
        The validated API key

    Raises:
        HTTPException: If authentication fails
    """
    # Get API keys from settings (comma-separated list)
    valid_keys = []
    if hasattr(settings, 'admin_api_keys') and settings.admin_api_keys:
        valid_keys = [k.strip() for k in settings.admin_api_keys.split(',') if k.strip()]

    if not valid_keys:
        logger.error("no_admin_api_keys_configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication not configured"
        )

    # Try X-API-Key header first
    if api_key:
        for valid_key in valid_keys:
            if constant_time_compare(api_key, valid_key):
                logger.info("api_key_authenticated", source="header")
                return api_key
        logger.warning("invalid_api_key_attempt", source="header")

    # Try Authorization: Bearer token
    if bearer and bearer.credentials:
        for valid_key in valid_keys:
            if constant_time_compare(bearer.credentials, valid_key):
                logger.info("api_key_authenticated", source="bearer")
                return bearer.credentials
        logger.warning("invalid_api_key_attempt", source="bearer")

    # No valid authentication provided
    logger.warning("authentication_missing_or_invalid")
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing API key",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def verify_api_key_optional(
    api_key: Optional[str] = Security(api_key_header),
    bearer: Optional[HTTPAuthorizationCredentials] = Security(bearer_header)
) -> Optional[str]:
    """
    Optional API key verification
    Returns None if no auth provided, validates if provided

    Args:
        api_key: API key from X-API-Key header
        bearer: Bearer token from Authorization header

    Returns:
        The validated API key or None

    Raises:
        HTTPException: If authentication is provided but invalid
    """
    # If no authentication provided at all, return None
    if not api_key and not bearer:
        return None

    # If authentication was attempted, verify it
    return await verify_api_key(api_key, bearer)


def generate_admin_api_key() -> str:
    """
    Generate a secure random API key for admin use

    Returns:
        A cryptographically secure random API key (64 characters)
    """
    return secrets.token_urlsafe(48)  # 48 bytes = 64 characters base64


def hash_api_key_for_logging(api_key: str) -> str:
    """
    Hash API key for safe logging
    Shows only first 8 characters

    Args:
        api_key: The API key to hash

    Returns:
        Partially redacted API key
    """
    if len(api_key) <= 8:
        return "****"
    return f"{api_key[:8]}...****"
