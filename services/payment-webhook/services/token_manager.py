"""
Token Manager Service
Handles token accounting, monthly resets, and token pack additions
"""
import aiosqlite
from datetime import datetime, timedelta
from typing import Dict, Tuple, Optional
import structlog

from config import settings
from utils.database import get_db, get_api_key_by_id, update_api_key, get_total_tokens

logger = structlog.get_logger()


async def get_available_tokens(api_key_id: int) -> Dict[str, int]:
    """
    Get total available tokens for an API key
    Checks if monthly reset is needed first

    Returns:
        {
            "monthly": int,
            "purchased": int,
            "total": int,
            "reset_needed": bool
        }
    """
    api_key = await get_api_key_by_id(api_key_id)
    if not api_key:
        return {"monthly": 0, "purchased": 0, "total": 0, "reset_needed": False}

    # Check if monthly reset is needed
    reset_date = api_key.get('monthly_reset_date')
    needs_reset = False

    if reset_date:
        reset_dt = datetime.fromisoformat(reset_date)
        if datetime.now() >= reset_dt:
            needs_reset = True
            await reset_monthly_tokens(api_key_id)
            # Re-fetch after reset
            api_key = await get_api_key_by_id(api_key_id)

    monthly = api_key.get('monthly_tokens_remaining', 0) or 0
    purchased = api_key.get('purchased_tokens_remaining', 0) or 0

    return {
        "monthly": monthly,
        "purchased": purchased,
        "total": monthly + purchased,
        "reset_needed": needs_reset
    }


async def reset_monthly_tokens(api_key_id: int) -> bool:
    """
    Reset monthly tokens to the allowance and set next reset date
    This happens on the monthly billing cycle
    """
    api_key = await get_api_key_by_id(api_key_id)
    if not api_key:
        return False

    # Calculate next reset date (30 days from now)
    next_reset = datetime.now() + timedelta(days=30)

    await update_api_key(
        api_key_id,
        monthly_tokens_remaining=settings.monthly_token_allowance,
        tokens_used_this_month=0,
        monthly_reset_date=next_reset.isoformat()
    )

    logger.info(
        "monthly_tokens_reset",
        api_key_id=api_key_id,
        email=api_key.get('client_email'),
        new_balance=settings.monthly_token_allowance,
        next_reset=next_reset.isoformat()
    )

    return True


async def add_purchased_tokens(
    api_key_id: int,
    tokens_to_add: int,
    pack_type: str,
    price_paid: float,
    currency: str = "USD"
) -> Tuple[bool, Dict[str, int]]:
    """
    Add purchased tokens to an API key
    These tokens never expire

    Returns:
        (success: bool, new_balances: dict)
    """
    api_key = await get_api_key_by_id(api_key_id)
    if not api_key:
        logger.error("add_purchased_tokens_failed", reason="api_key_not_found", api_key_id=api_key_id)
        return False, {}

    current_purchased = api_key.get('purchased_tokens_remaining', 0) or 0
    new_purchased = current_purchased + tokens_to_add

    await update_api_key(
        api_key_id,
        purchased_tokens_remaining=new_purchased
    )

    # Get updated balances
    balances = await get_available_tokens(api_key_id)

    logger.info(
        "purchased_tokens_added",
        api_key_id=api_key_id,
        email=api_key.get('client_email'),
        pack_type=pack_type,
        tokens_added=tokens_to_add,
        price_paid=price_paid,
        currency=currency,
        new_purchased_balance=new_purchased,
        total_balance=balances['total']
    )

    return True, balances


async def deduct_tokens(api_key_id: int, tokens_used: int) -> Tuple[bool, Dict[str, int]]:
    """
    Deduct tokens from available balance
    Deducts from monthly first, then purchased

    Returns:
        (success: bool, remaining_balances: dict)
    """
    api_key = await get_api_key_by_id(api_key_id)
    if not api_key:
        return False, {}

    # Get current balances (with auto-reset if needed)
    balances = await get_available_tokens(api_key_id)

    if balances['total'] < tokens_used:
        logger.warning(
            "insufficient_tokens",
            api_key_id=api_key_id,
            email=api_key.get('client_email'),
            required=tokens_used,
            available=balances['total']
        )
        return False, balances

    monthly = balances['monthly']
    purchased = balances['purchased']
    tokens_used_this_month = api_key.get('tokens_used_this_month', 0) or 0

    # Deduct from monthly first
    if monthly >= tokens_used:
        new_monthly = monthly - tokens_used
        new_purchased = purchased
        tokens_used_this_month += tokens_used
    else:
        # Use all monthly, then deduct from purchased
        remaining_to_deduct = tokens_used - monthly
        new_monthly = 0
        new_purchased = purchased - remaining_to_deduct
        tokens_used_this_month += monthly

    await update_api_key(
        api_key_id,
        monthly_tokens_remaining=new_monthly,
        purchased_tokens_remaining=new_purchased,
        tokens_used_this_month=tokens_used_this_month,
        last_token_usage=datetime.now().isoformat()
    )

    new_balances = {
        "monthly": new_monthly,
        "purchased": new_purchased,
        "total": new_monthly + new_purchased
    }

    logger.info(
        "tokens_deducted",
        api_key_id=api_key_id,
        email=api_key.get('client_email'),
        tokens_used=tokens_used,
        new_balances=new_balances
    )

    return True, new_balances


async def initialize_token_account(api_key_id: int, monthly_allowance: Optional[int] = None) -> bool:
    """
    Initialize token accounting for a new API key
    Sets monthly allowance and first reset date
    """
    allowance = monthly_allowance or settings.monthly_token_allowance
    next_reset = datetime.now() + timedelta(days=30)

    await update_api_key(
        api_key_id,
        monthly_token_allowance=allowance,
        monthly_tokens_remaining=allowance,
        purchased_tokens_remaining=0,
        tokens_used_this_month=0,
        monthly_reset_date=next_reset.isoformat()
    )

    logger.info(
        "token_account_initialized",
        api_key_id=api_key_id,
        monthly_allowance=allowance,
        next_reset=next_reset.isoformat()
    )

    return True


async def check_token_balance_health(api_key_id: int) -> Dict[str, any]:
    """
    Check token balance health and return warnings if needed
    Useful for sending low balance notifications
    """
    balances = await get_available_tokens(api_key_id)
    api_key = await get_api_key_by_id(api_key_id)

    if not api_key:
        return {"healthy": False, "reason": "api_key_not_found"}

    total = balances['total']
    allowance = api_key.get('monthly_token_allowance', settings.monthly_token_allowance)

    # Calculate percentage remaining
    if allowance > 0:
        monthly_percent = (balances['monthly'] / allowance) * 100
    else:
        monthly_percent = 0

    health = {
        "healthy": True,
        "total_tokens": total,
        "monthly_tokens": balances['monthly'],
        "purchased_tokens": balances['purchased'],
        "monthly_percent_remaining": monthly_percent,
        "warnings": []
    }

    # Check for low balance warnings
    if total == 0:
        health["healthy"] = False
        health["warnings"].append("no_tokens_remaining")
    elif total < 10000:
        health["warnings"].append("critically_low")
    elif monthly_percent < 10 and balances['purchased'] == 0:
        health["warnings"].append("monthly_tokens_low_no_purchased")
    elif monthly_percent < 25:
        health["warnings"].append("monthly_tokens_below_25_percent")

    return health


async def get_token_usage_stats(api_key_id: int) -> Dict[str, any]:
    """
    Get token usage statistics for an API key
    """
    api_key = await get_api_key_by_id(api_key_id)
    if not api_key:
        return {}

    balances = await get_available_tokens(api_key_id)
    allowance = api_key.get('monthly_token_allowance', settings.monthly_token_allowance)
    used_this_month = api_key.get('tokens_used_this_month', 0) or 0

    return {
        "monthly_allowance": allowance,
        "monthly_remaining": balances['monthly'],
        "monthly_used": used_this_month,
        "monthly_usage_percent": (used_this_month / allowance * 100) if allowance > 0 else 0,
        "purchased_tokens": balances['purchased'],
        "total_available": balances['total'],
        "last_usage": api_key.get('last_token_usage'),
        "next_reset": api_key.get('monthly_reset_date')
    }
