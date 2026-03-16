"""
API Key Provisioner Service
Creates and manages API keys for customers after successful payment
"""
import hashlib
import secrets
import string
from datetime import datetime, timedelta
from typing import Optional, Dict, Tuple
import structlog

from config import settings
from utils.database import get_db, get_api_key_by_email, update_api_key
from services.token_manager import initialize_token_account

logger = structlog.get_logger()


def generate_api_key(length: int = 50) -> str:
    """Generate a secure random API key with pg_ prefix"""
    alphabet = string.ascii_letters + string.digits
    random_part = ''.join(secrets.choice(alphabet) for _ in range(length))
    return f"pg_{random_part}"


def hash_key(key: str) -> str:
    """Hash the API key using SHA256"""
    return hashlib.sha256(key.encode('utf-8')).hexdigest()


async def provision_api_key(
    client_email: str,
    plan_tier: str,
    payment_method: str,
    subscription_id: Optional[str] = None,
    customer_id: Optional[str] = None,
    promo_code: Optional[str] = None,
    pgp_public_key: Optional[str] = None,
    pgp_fingerprint: Optional[str] = None,
    client_name: Optional[str] = None,
    monthly_price: Optional[float] = None
) -> Tuple[bool, Optional[str], Optional[int]]:
    """
    Provision a new API key for a customer after successful payment

    Args:
        client_email: Customer's email address
        plan_tier: Plan tier (trial, family, regular, ultra_privacy, beta)
        payment_method: Payment method used (stripe, strike_usd, strike_btc, btcpay)
        subscription_id: Provider's subscription ID (if subscription)
        customer_id: Provider's customer ID
        promo_code: Promo code used (if any)
        pgp_public_key: PGP public key for ultra privacy users
        pgp_fingerprint: PGP key fingerprint
        client_name: Optional client name (defaults to email)
        monthly_price: Price paid (gets from settings if not provided)

    Returns:
        (success: bool, api_key: str | None, api_key_id: int | None)
    """
    # Check if user already has an API key
    existing_key = await get_api_key_by_email(client_email)
    if existing_key and existing_key.get('is_active'):
        logger.warning(
            "api_key_already_exists",
            email=client_email,
            existing_key_id=existing_key['id']
        )
        # Update existing key with new subscription details
        await update_existing_key(
            existing_key['id'],
            plan_tier=plan_tier,
            payment_method=payment_method,
            subscription_id=subscription_id,
            customer_id=customer_id,
            promo_code=promo_code,
            monthly_price=monthly_price or settings.get_plan_price(plan_tier)
        )
        return True, None, existing_key['id']  # Don't return key for security

    # Generate new API key
    api_key = generate_api_key()
    key_hash = hash_key(api_key)

    # Determine price
    if monthly_price is None:
        monthly_price = settings.get_plan_price(plan_tier)

    # Determine client name
    if client_name is None:
        client_name = client_email.split('@')[0]  # Use email prefix as name

    # Set expiration (None = never expires for subscriptions)
    expires_at = None
    if plan_tier == 'trial':
        # Trial expires after 30 days
        expires_at = (datetime.now() + timedelta(days=30)).isoformat()

    # Create API key record
    async with get_db() as db:
        cursor = await db.execute(
            """
            INSERT INTO api_keys (
                key_hash, key_prefix, client_name, client_email,
                plan_type, plan_tier, promo_code,
                monthly_price, payment_method,
                payment_provider_customer_id, subscription_id, subscription_status,
                pgp_public_key, pgp_fingerprint,
                monthly_token_allowance, tokens_used, tokens_remaining,
                monthly_reset_date, created_at, expires_at, is_active, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key_hash,
                "pg_",
                client_name,
                client_email,
                plan_tier,  # plan_type (legacy field)
                plan_tier,  # plan_tier
                promo_code,
                monthly_price,
                payment_method,
                customer_id,
                subscription_id,
                "active",
                pgp_public_key,
                pgp_fingerprint,
                settings.monthly_token_allowance,
                0,  # tokens_used
                settings.monthly_token_allowance,  # tokens_remaining (legacy)
                (datetime.now() + timedelta(days=30)).isoformat(),  # monthly_reset_date
                datetime.now().isoformat(),  # created_at
                expires_at,
                1,  # is_active
                f"Created via {payment_method} payment"
            )
        )

        api_key_id = cursor.lastrowid

    # Initialize token accounting
    await initialize_token_account(api_key_id)

    logger.info(
        "api_key_provisioned",
        api_key_id=api_key_id,
        email=client_email,
        plan_tier=plan_tier,
        payment_method=payment_method,
        monthly_price=monthly_price,
        promo_code=promo_code
    )

    return True, api_key, api_key_id


async def update_existing_key(
    api_key_id: int,
    plan_tier: Optional[str] = None,
    payment_method: Optional[str] = None,
    subscription_id: Optional[str] = None,
    customer_id: Optional[str] = None,
    promo_code: Optional[str] = None,
    monthly_price: Optional[float] = None,
    subscription_status: str = "active"
) -> bool:
    """
    Update an existing API key with new subscription details
    Used when a user re-subscribes or changes plans
    """
    updates = {
        "subscription_status": subscription_status,
        "is_active": 1
    }

    if plan_tier:
        updates["plan_tier"] = plan_tier
        updates["plan_type"] = plan_tier  # Legacy field

    if payment_method:
        updates["payment_method"] = payment_method

    if subscription_id:
        updates["subscription_id"] = subscription_id

    if customer_id:
        updates["payment_provider_customer_id"] = customer_id

    if promo_code:
        updates["promo_code"] = promo_code

    if monthly_price:
        updates["monthly_price"] = monthly_price

    await update_api_key(api_key_id, **updates)

    logger.info(
        "api_key_updated",
        api_key_id=api_key_id,
        updates=list(updates.keys())
    )

    return True


async def deactivate_api_key(api_key_id: int, reason: str = "subscription_canceled") -> bool:
    """
    Deactivate an API key (subscription canceled or expired)
    """
    await update_api_key(
        api_key_id,
        is_active=0,
        subscription_status="canceled",
        notes=f"Deactivated: {reason}"
    )

    logger.info(
        "api_key_deactivated",
        api_key_id=api_key_id,
        reason=reason
    )

    return True


async def reactivate_api_key(api_key_id: int, subscription_id: Optional[str] = None) -> bool:
    """
    Reactivate an API key (subscription renewed)
    """
    updates = {
        "is_active": 1,
        "subscription_status": "active"
    }

    if subscription_id:
        updates["subscription_id"] = subscription_id

    await update_api_key(api_key_id, **updates)

    # Reset monthly tokens
    from services.token_manager import reset_monthly_tokens
    await reset_monthly_tokens(api_key_id)

    logger.info(
        "api_key_reactivated",
        api_key_id=api_key_id,
        subscription_id=subscription_id
    )

    return True


async def upgrade_downgrade_plan(
    api_key_id: int,
    new_plan_tier: str,
    new_monthly_price: float,
    new_subscription_id: Optional[str] = None
) -> bool:
    """
    Upgrade or downgrade a user's plan
    """
    updates = {
        "plan_tier": new_plan_tier,
        "plan_type": new_plan_tier,  # Legacy
        "monthly_price": new_monthly_price
    }

    if new_subscription_id:
        updates["subscription_id"] = new_subscription_id

    await update_api_key(api_key_id, **updates)

    logger.info(
        "plan_changed",
        api_key_id=api_key_id,
        new_plan_tier=new_plan_tier,
        new_price=new_monthly_price
    )

    return True
