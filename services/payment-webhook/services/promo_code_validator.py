"""
Promo Code Validator Service
Validates and applies promotional codes
"""
from datetime import datetime
from typing import Optional, Dict, Tuple
import structlog

from config import settings
from utils.database import get_promo_code, increment_promo_usage

logger = structlog.get_logger()


async def validate_promo_code(
    code: str,
    plan_tier: str
) -> Tuple[bool, Optional[Dict], Optional[str]]:
    """
    Validate a promo code

    Args:
        code: Promo code to validate
        plan_tier: Plan tier the user is trying to purchase

    Returns:
        (valid: bool, promo_details: dict | None, error_message: str | None)
    """
    if not code:
        return False, None, "Promo code is required"

    # Get promo code from database
    promo = await get_promo_code(code.upper())

    if not promo:
        logger.warning("promo_code_not_found", code=code)
        return False, None, "Invalid promo code"

    # Check if active
    if not promo.get('is_active'):
        logger.warning("promo_code_inactive", code=code)
        return False, None, "This promo code is no longer active"

    # Check expiration
    expires_at = promo.get('expires_at')
    if expires_at:
        expiry_dt = datetime.fromisoformat(expires_at)
        if datetime.now() > expiry_dt:
            logger.warning("promo_code_expired", code=code, expired_at=expires_at)
            return False, None, "This promo code has expired"

    # Check usage limit
    max_uses = promo.get('max_uses')
    current_uses = promo.get('current_uses', 0)
    if max_uses is not None and current_uses >= max_uses:
        logger.warning("promo_code_max_uses_reached", code=code, max_uses=max_uses)
        return False, None, "This promo code has reached its usage limit"

    # Check if promo code is valid for this plan tier
    promo_plan_tier = promo.get('plan_tier')
    if promo_plan_tier and promo_plan_tier != plan_tier:
        logger.warning(
            "promo_code_wrong_plan",
            code=code,
            promo_plan=promo_plan_tier,
            requested_plan=plan_tier
        )
        return False, None, f"This promo code is only valid for the '{promo_plan_tier}' plan"

    # Validation passed
    logger.info("promo_code_validated", code=code, plan_tier=plan_tier)

    return True, dict(promo), None


async def apply_promo_code(
    code: str,
    plan_tier: str,
    base_price: float
) -> Tuple[bool, float, Optional[Dict]]:
    """
    Apply a promo code and calculate the final price

    Args:
        code: Promo code to apply
        plan_tier: Plan tier
        base_price: Base price before discount

    Returns:
        (success: bool, final_price: float, promo_details: dict | None)
    """
    # Validate the promo code
    valid, promo, error = await validate_promo_code(code, plan_tier)

    if not valid:
        logger.warning("promo_code_application_failed", code=code, error=error)
        return False, base_price, None

    # Calculate discount
    discount_amount = promo.get('discount_amount', 0) or 0
    discount_percent = promo.get('discount_percent', 0) or 0

    final_price = base_price

    if discount_amount > 0:
        # Fixed dollar discount
        final_price = max(0, base_price - discount_amount)
    elif discount_percent > 0:
        # Percentage discount
        discount_value = base_price * (discount_percent / 100)
        final_price = max(0, base_price - discount_value)

    # Increment usage count
    await increment_promo_usage(promo['id'])

    logger.info(
        "promo_code_applied",
        code=code,
        base_price=base_price,
        final_price=final_price,
        discount_amount=discount_amount,
        discount_percent=discount_percent
    )

    return True, final_price, dict(promo)


async def get_promo_details(code: str) -> Optional[Dict]:
    """
    Get promo code details without validation
    Useful for displaying promo info to users
    """
    promo = await get_promo_code(code.upper())
    if not promo:
        return None

    # Calculate what discount this provides
    discount_amount = promo.get('discount_amount', 0) or 0
    discount_percent = promo.get('discount_percent', 0) or 0

    plan_tier = promo.get('plan_tier')
    base_price = settings.get_plan_price(plan_tier) if plan_tier else 0

    if discount_amount > 0:
        final_price = max(0, base_price - discount_amount)
        discount_type = "fixed"
        discount_display = f"${discount_amount:.2f} off"
    elif discount_percent > 0:
        final_price = base_price * (1 - discount_percent / 100)
        discount_type = "percent"
        discount_display = f"{discount_percent}% off"
    else:
        final_price = base_price
        discount_type = "none"
        discount_display = "No discount"

    return {
        "code": promo['code'],
        "plan_tier": plan_tier,
        "valid": promo.get('is_active', False),
        "discount_type": discount_type,
        "discount_display": discount_display,
        "base_price": base_price,
        "final_price": final_price,
        "savings": base_price - final_price,
        "expires_at": promo.get('expires_at'),
        "uses_remaining": (promo.get('max_uses') - promo.get('current_uses', 0)) if promo.get('max_uses') else None
    }


def calculate_price_with_promo(plan_tier: str, promo_code: Optional[str] = None) -> float:
    """
    Calculate final price for a plan tier with optional promo code
    Synchronous helper for quick price checks

    Args:
        plan_tier: Plan tier
        promo_code: Optional promo code

    Returns:
        Final price (uses base price if promo invalid)
    """
    base_price = settings.get_plan_price(plan_tier)

    # If no promo code, return base price
    if not promo_code:
        return base_price

    # For now, return base price (async validation needed for full check)
    # This is a sync helper for display purposes
    return base_price
