"""
Database Utilities
Helper functions for database operations
"""
import aiosqlite
from typing import Optional, Dict, Any, List
from datetime import datetime
from contextlib import asynccontextmanager
import structlog

from config import settings
from utils.security import (
    validate_email,
    sanitize_email,
    sanitize_string,
    validate_subscription_id,
    validate_amount,
    validate_currency
)

logger = structlog.get_logger()


@asynccontextmanager
async def get_db():
    """Async context manager for database connections"""
    conn = await aiosqlite.connect(settings.key_db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.close()


async def ensure_event_deduplication_table():
    """Create table for tracking processed webhook events"""
    async with get_db() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS processed_webhook_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                payment_method TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                event_hash TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_event_id ON processed_webhook_events(event_id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_processed_at ON processed_webhook_events(processed_at)
        """)


async def is_event_already_processed(event_id: str, payment_method: str) -> bool:
    """Check if webhook event has already been processed"""
    if not event_id:
        return False

    async with get_db() as db:
        async with db.execute(
            "SELECT id FROM processed_webhook_events WHERE event_id = ? AND payment_method = ?",
            (event_id, payment_method)
        ) as cursor:
            row = await cursor.fetchone()
            return row is not None


async def mark_event_as_processed(event_id: str, event_type: str, payment_method: str) -> bool:
    """Mark webhook event as processed to prevent replay"""
    if not event_id:
        return False

    try:
        import hashlib
        event_hash = hashlib.sha256(f"{event_id}:{payment_method}:{event_type}".encode()).hexdigest()

        async with get_db() as db:
            await db.execute(
                """INSERT INTO processed_webhook_events
                   (event_id, event_type, payment_method, processed_at, event_hash)
                   VALUES (?, ?, ?, ?, ?)""",
                (event_id, event_type, payment_method, datetime.now().isoformat(), event_hash)
            )
        return True
    except Exception as e:
        logger.error("failed_to_mark_event_processed", error=str(e), event_id=event_id)
        return False


async def get_api_key_by_id(api_key_id: int) -> Optional[Dict[str, Any]]:
    """Get API key record by ID"""
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM api_keys WHERE id = ?", (api_key_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_api_key_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Get API key record by client email"""
    # Validate and sanitize email
    valid, error = validate_email(email)
    if not valid:
        logger.warning("invalid_email_lookup", email=email, error=error)
        return None

    email = sanitize_email(email)

    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM api_keys WHERE client_email = ? AND is_active = 1",
            (email,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_api_key_by_hash(key_hash: str) -> Optional[Dict[str, Any]]:
    """Get API key record by key hash"""
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM api_keys WHERE key_hash = ? AND is_active = 1",
            (key_hash,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_api_key_by_subscription_id(subscription_id: str, payment_method: str) -> Optional[Dict[str, Any]]:
    """Get API key by subscription ID and payment method"""
    async with get_db() as db:
        async with db.execute(
            """SELECT * FROM api_keys
               WHERE subscription_id = ? AND payment_method = ? AND is_active = 1""",
            (subscription_id, payment_method)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def update_api_key(api_key_id: int, **kwargs) -> bool:
    """Update API key fields"""
    if not kwargs:
        return False

    # Whitelist of allowed columns to prevent SQL injection
    ALLOWED_COLUMNS = {
        'plan_tier', 'promo_code', 'monthly_price', 'payment_method',
        'payment_provider_customer_id', 'subscription_id', 'subscription_status',
        'pgp_fingerprint', 'pgp_public_key', 'monthly_tokens_remaining',
        'purchased_tokens_remaining', 'tokens_used_this_month', 'is_active',
        'monthly_reset_date', 'last_token_usage', 'expires_at', 'notes',
        'client_name', 'client_email', 'monthly_token_allowance'
    }

    # Filter to only allowed columns
    safe_updates = {k: v for k, v in kwargs.items() if k in ALLOWED_COLUMNS}

    if not safe_updates:
        return False

    # Build SET clause from whitelisted columns
    set_clause = ", ".join(f"{key} = ?" for key in safe_updates.keys())
    values = list(safe_updates.values()) + [api_key_id]

    async with get_db() as db:
        await db.execute(
            f"UPDATE api_keys SET {set_clause} WHERE id = ?",
            values
        )
        return True


async def log_payment_event(
    payment_type: str,
    payment_method: str,
    event_type: str,
    customer_email: str,
    amount: float,
    currency: str,
    **kwargs
) -> int:
    """Log a payment event and return its ID"""
    # Validate required inputs
    valid, error = validate_email(customer_email)
    if not valid:
        logger.error("invalid_email_in_payment_event", email=customer_email, error=error)
        raise ValueError(f"Invalid email: {error}")

    valid, error = validate_amount(amount, min_amount=0.0, max_amount=100000.0)
    if not valid:
        logger.error("invalid_amount_in_payment_event", amount=amount, error=error)
        raise ValueError(f"Invalid amount: {error}")

    valid, error = validate_currency(currency)
    if not valid:
        logger.error("invalid_currency_in_payment_event", currency=currency, error=error)
        raise ValueError(f"Invalid currency: {error}")

    # Sanitize inputs
    customer_email = sanitize_email(customer_email)
    payment_type = sanitize_string(payment_type, 50)
    payment_method = sanitize_string(payment_method, 50)
    event_type = sanitize_string(event_type, 50)

    # Sanitize optional kwargs
    invoice_id = sanitize_string(kwargs.get('invoice_id', ''), 255) if kwargs.get('invoice_id') else None
    event_id = sanitize_string(kwargs.get('event_id', ''), 255) if kwargs.get('event_id') else None
    plan_tier = sanitize_string(kwargs.get('plan_tier', ''), 50) if kwargs.get('plan_tier') else None
    promo_code = sanitize_string(kwargs.get('promo_code', ''), 50) if kwargs.get('promo_code') else None
    pack_type = sanitize_string(kwargs.get('pack_type', ''), 50) if kwargs.get('pack_type') else None
    provider_payment_id = sanitize_string(kwargs.get('provider_payment_id', ''), 255) if kwargs.get('provider_payment_id') else None
    provider_subscription_id = sanitize_string(kwargs.get('provider_subscription_id', ''), 255) if kwargs.get('provider_subscription_id') else None
    status = sanitize_string(kwargs.get('status', 'pending'), 50)
    payload = sanitize_string(kwargs.get('payload', ''), 10000) if kwargs.get('payload') else None

    # Validate numeric inputs
    api_key_id = kwargs.get('api_key_id')
    if api_key_id is not None and not isinstance(api_key_id, int):
        raise ValueError("api_key_id must be an integer")

    tokens_added = kwargs.get('tokens_added')
    if tokens_added is not None and not isinstance(tokens_added, int):
        raise ValueError("tokens_added must be an integer")

    async with get_db() as db:
        cursor = await db.execute(
            """
            INSERT INTO payment_events (
                payment_type, payment_method, event_type, event_id,
                customer_email, amount, currency, api_key_id,
                plan_tier, promo_code, pack_type, tokens_added,
                provider_payment_id, provider_subscription_id,
                status, created_at, payload, invoice_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payment_type,
                payment_method,
                event_type,
                event_id,
                customer_email,
                amount,
                currency,
                api_key_id,
                plan_tier,
                promo_code,
                pack_type,
                tokens_added,
                provider_payment_id,
                provider_subscription_id,
                status,
                datetime.now().isoformat(),
                payload,
                invoice_id
            )
        )
        return cursor.lastrowid


async def update_payment_event(event_id: int, **kwargs) -> bool:
    """Update payment event fields"""
    if not kwargs:
        return False

    # Whitelist of allowed columns to prevent SQL injection
    ALLOWED_COLUMNS = {
        'api_key_id', 'status', 'completed_at', 'payload',
        'event_type', 'provider_payment_id', 'provider_subscription_id',
        'amount', 'currency', 'tokens_added'
    }

    # Filter to only allowed columns
    safe_updates = {k: v for k, v in kwargs.items() if k in ALLOWED_COLUMNS}

    if not safe_updates:
        return False

    # Build SET clause from whitelisted columns
    set_clause = ", ".join(f"{key} = ?" for key in safe_updates.keys())
    values = list(safe_updates.values()) + [event_id]

    async with get_db() as db:
        await db.execute(
            f"UPDATE payment_events SET {set_clause} WHERE id = ?",
            values
        )
        return True


async def log_token_purchase(
    api_key_id: int,
    pack_type: str,
    tokens_purchased: int,
    price_paid: float,
    currency: str,
    payment_method: str,
    payment_event_id: Optional[int] = None
) -> int:
    """Log a token pack purchase"""
    async with get_db() as db:
        cursor = await db.execute(
            """
            INSERT INTO token_purchases (
                api_key_id, pack_type, tokens_purchased,
                price_paid, currency, payment_method,
                payment_event_id, purchased_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                api_key_id,
                pack_type,
                tokens_purchased,
                price_paid,
                currency,
                payment_method,
                payment_event_id,
                datetime.now().isoformat()
            )
        )
        return cursor.lastrowid


async def get_promo_code(code: str) -> Optional[Dict[str, Any]]:
    """Get promo code details"""
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM promo_codes WHERE code = ? AND is_active = 1",
            (code,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def increment_promo_usage(promo_code_id: int) -> bool:
    """Increment promo code usage count"""
    async with get_db() as db:
        await db.execute(
            "UPDATE promo_codes SET current_uses = current_uses + 1 WHERE id = ?",
            (promo_code_id,)
        )
        return True


async def get_payment_event_by_provider_id(provider_payment_id: str, payment_method: str) -> Optional[Dict[str, Any]]:
    """Get payment event by provider's payment ID"""
    async with get_db() as db:
        async with db.execute(
            """SELECT * FROM payment_events
               WHERE provider_payment_id = ? AND payment_method = ?
               ORDER BY created_at DESC LIMIT 1""",
            (provider_payment_id, payment_method)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_total_tokens(api_key_id: int) -> Dict[str, int]:
    """Get total available tokens for an API key"""
    async with get_db() as db:
        async with db.execute(
            """SELECT monthly_tokens_remaining, purchased_tokens_remaining
               FROM api_keys WHERE id = ?""",
            (api_key_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "monthly": row[0] or 0,
                    "purchased": row[1] or 0,
                    "total": (row[0] or 0) + (row[1] or 0)
                }
            return {"monthly": 0, "purchased": 0, "total": 0}


async def get_token_purchases_for_key(api_key_id: int) -> List[Dict[str, Any]]:
    """Get all token purchases for an API key"""
    async with get_db() as db:
        async with db.execute(
            """SELECT * FROM token_purchases
               WHERE api_key_id = ?
               ORDER BY purchased_at DESC""",
            (api_key_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def create_invoice(
    invoice_id: str,
    customer_email: str,
    amount: float,
    currency: str,
    payment_type: str,
    payment_method: str,
    **kwargs
) -> int:
    """
    Create a new invoice (payment_event) record
    Returns the payment_event ID
    """
    event_id = await log_payment_event(
        payment_type=payment_type,
        payment_method=payment_method,
        event_type='created',
        customer_email=customer_email,
        amount=amount,
        currency=currency,
        invoice_id=invoice_id,
        status='pending',
        **kwargs
    )
    logger.info("invoice_created", invoice_id=invoice_id, event_id=event_id)
    return event_id


async def get_invoice_by_id(invoice_id: str) -> Optional[Dict[str, Any]]:
    """Get invoice (payment_event) by invoice_id"""
    invoice_id = sanitize_string(invoice_id, 255)

    async with get_db() as db:
        async with db.execute(
            """SELECT * FROM payment_events
               WHERE invoice_id = ?
               ORDER BY created_at DESC LIMIT 1""",
            (invoice_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def update_invoice_status(invoice_id: str, status: str, **kwargs) -> bool:
    """Update invoice status"""
    invoice_id = sanitize_string(invoice_id, 255)
    status = sanitize_string(status, 50)

    async with get_db() as db:
        # First get the invoice
        async with db.execute(
            "SELECT id FROM payment_events WHERE invoice_id = ?",
            (invoice_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False

            event_id = row[0]

        # Update the invoice
        return await update_payment_event(event_id, status=status, **kwargs)
