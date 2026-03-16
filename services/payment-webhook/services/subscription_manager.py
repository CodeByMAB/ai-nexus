"""
Subscription Manager Service
High-level service for managing subscription lifecycle
"""
from typing import Optional, Dict, Tuple
import structlog

from config import settings
from services.key_provisioner import (
    provision_api_key,
    update_existing_key,
    deactivate_api_key,
    reactivate_api_key,
    upgrade_downgrade_plan
)
from services.token_manager import reset_monthly_tokens, add_purchased_tokens
from services.promo_code_validator import apply_promo_code
from utils.database import (
    get_api_key_by_email,
    get_api_key_by_subscription_id,
    log_payment_event,
    log_token_purchase,
    update_payment_event
)

logger = structlog.get_logger()


async def handle_new_subscription(
    customer_email: str,
    plan_tier: str,
    payment_method: str,
    amount: float,
    currency: str,
    subscription_id: str,
    customer_id: str,
    promo_code: Optional[str] = None,
    pgp_public_key: Optional[str] = None,
    provider_payment_id: Optional[str] = None,
    event_id: Optional[str] = None
) -> Tuple[bool, Optional[str], Optional[int], Optional[int]]:
    """
    Handle a new subscription payment
    Creates API key and sets up token accounting

    Returns:
        (success: bool, api_key: str | None, api_key_id: int | None, payment_event_id: int | None)
    """
    # Apply promo code if provided
    final_price = amount
    if promo_code:
        success, final_price, promo_details = await apply_promo_code(promo_code, plan_tier, amount)
        if not success:
            logger.warning("promo_code_failed", code=promo_code, email=customer_email)
            promo_code = None  # Continue without promo

    # Log payment event
    payment_event_id = await log_payment_event(
        payment_type="subscription",
        payment_method=payment_method,
        event_type="completed",
        customer_email=customer_email,
        amount=final_price,
        currency=currency,
        plan_tier=plan_tier,
        promo_code=promo_code,
        provider_payment_id=provider_payment_id,
        provider_subscription_id=subscription_id,
        event_id=event_id,
        status="completed"
    )

    # Provision API key
    success, api_key, api_key_id = await provision_api_key(
        client_email=customer_email,
        plan_tier=plan_tier,
        payment_method=payment_method,
        subscription_id=subscription_id,
        customer_id=customer_id,
        promo_code=promo_code,
        pgp_public_key=pgp_public_key,
        monthly_price=final_price
    )

    if success and api_key_id:
        # Update payment event with API key ID
        await update_payment_event(payment_event_id, api_key_id=api_key_id)

        logger.info(
            "subscription_created",
            email=customer_email,
            plan_tier=plan_tier,
            payment_method=payment_method,
            api_key_id=api_key_id,
            amount=final_price,
            subscription_id=subscription_id
        )

    return success, api_key, api_key_id, payment_event_id


async def handle_subscription_renewal(
    subscription_id: str,
    payment_method: str,
    amount: float,
    currency: str,
    provider_payment_id: Optional[str] = None,
    event_id: Optional[str] = None
) -> Tuple[bool, Optional[int]]:
    """
    Handle a subscription renewal payment
    Resets monthly tokens for the billing cycle

    Returns:
        (success: bool, api_key_id: int | None)
    """
    # Find API key by subscription ID
    api_key = await get_api_key_by_subscription_id(subscription_id, payment_method)

    if not api_key:
        logger.error(
            "subscription_renewal_failed",
            reason="api_key_not_found",
            subscription_id=subscription_id,
            payment_method=payment_method
        )
        return False, None

    api_key_id = api_key['id']
    customer_email = api_key['client_email']

    # Log payment event
    payment_event_id = await log_payment_event(
        payment_type="subscription",
        payment_method=payment_method,
        event_type="renewed",
        customer_email=customer_email,
        amount=amount,
        currency=currency,
        api_key_id=api_key_id,
        plan_tier=api_key.get('plan_tier'),
        provider_payment_id=provider_payment_id,
        provider_subscription_id=subscription_id,
        event_id=event_id,
        status="completed"
    )

    # Reset monthly tokens
    await reset_monthly_tokens(api_key_id)

    # Ensure key is active
    await update_existing_key(
        api_key_id,
        subscription_status="active"
    )

    logger.info(
        "subscription_renewed",
        api_key_id=api_key_id,
        email=customer_email,
        subscription_id=subscription_id,
        amount=amount
    )

    return True, api_key_id


async def handle_subscription_canceled(
    subscription_id: str,
    payment_method: str,
    reason: str = "customer_canceled"
) -> Tuple[bool, Optional[int]]:
    """
    Handle subscription cancellation
    Deactivates API key (but tokens remain until expiry)

    Returns:
        (success: bool, api_key_id: int | None)
    """
    # Find API key
    api_key = await get_api_key_by_subscription_id(subscription_id, payment_method)

    if not api_key:
        logger.error(
            "subscription_cancel_failed",
            reason="api_key_not_found",
            subscription_id=subscription_id
        )
        return False, None

    api_key_id = api_key['id']
    customer_email = api_key['client_email']

    # Log event
    await log_payment_event(
        payment_type="subscription",
        payment_method=payment_method,
        event_type="canceled",
        customer_email=customer_email,
        amount=0,
        currency="USD",
        api_key_id=api_key_id,
        provider_subscription_id=subscription_id,
        status="canceled"
    )

    # Deactivate API key
    await deactivate_api_key(api_key_id, reason=reason)

    logger.info(
        "subscription_canceled",
        api_key_id=api_key_id,
        email=customer_email,
        subscription_id=subscription_id,
        reason=reason
    )

    return True, api_key_id


async def handle_token_pack_purchase(
    customer_email: str,
    pack_type: str,
    payment_method: str,
    amount: float,
    currency: str,
    provider_payment_id: str,
    event_id: Optional[str] = None
) -> Tuple[bool, Optional[int], Optional[int]]:
    """
    Handle token pack purchase
    Adds purchased tokens to user's account

    Returns:
        (success: bool, api_key_id: int | None, tokens_added: int | None)
    """
    # Get pack details
    pack_details = settings.get_pack_details(pack_type)
    if not pack_details:
        logger.error("invalid_pack_type", pack_type=pack_type)
        return False, None, None

    tokens_to_add = pack_details['tokens']
    pack_price = pack_details['price']

    # Find user's API key
    api_key = await get_api_key_by_email(customer_email)

    if not api_key:
        logger.error(
            "token_purchase_failed",
            reason="api_key_not_found",
            email=customer_email
        )
        return False, None, None

    api_key_id = api_key['id']

    # Log payment event
    payment_event_id = await log_payment_event(
        payment_type="token_pack",
        payment_method=payment_method,
        event_type="completed",
        customer_email=customer_email,
        amount=amount,
        currency=currency,
        api_key_id=api_key_id,
        pack_type=pack_type,
        tokens_added=tokens_to_add,
        provider_payment_id=provider_payment_id,
        event_id=event_id,
        status="completed"
    )

    # Add purchased tokens
    success, balances = await add_purchased_tokens(
        api_key_id=api_key_id,
        tokens_to_add=tokens_to_add,
        pack_type=pack_type,
        price_paid=amount,
        currency=currency
    )

    if success:
        # Log token purchase
        await log_token_purchase(
            api_key_id=api_key_id,
            pack_type=pack_type,
            tokens_purchased=tokens_to_add,
            price_paid=amount,
            currency=currency,
            payment_method=payment_method,
            payment_event_id=payment_event_id
        )

        logger.info(
            "token_pack_purchased",
            api_key_id=api_key_id,
            email=customer_email,
            pack_type=pack_type,
            tokens_added=tokens_to_add,
            new_balance=balances.get('total'),
            amount=amount
        )

    return success, api_key_id, tokens_to_add


async def handle_payment_failed(
    subscription_id: str,
    payment_method: str,
    reason: str = "payment_failed"
) -> Tuple[bool, Optional[int]]:
    """
    Handle failed payment
    Updates subscription status but doesn't immediately deactivate

    Returns:
        (success: bool, api_key_id: int | None)
    """
    # Find API key
    api_key = await get_api_key_by_subscription_id(subscription_id, payment_method)

    if not api_key:
        logger.error(
            "payment_failed_handler_error",
            reason="api_key_not_found",
            subscription_id=subscription_id
        )
        return False, None

    api_key_id = api_key['id']
    customer_email = api_key['client_email']

    # Log event
    await log_payment_event(
        payment_type="subscription",
        payment_method=payment_method,
        event_type="failed",
        customer_email=customer_email,
        amount=0,
        currency="USD",
        api_key_id=api_key_id,
        provider_subscription_id=subscription_id,
        status="failed"
    )

    # Update subscription status to past_due
    await update_existing_key(
        api_key_id,
        subscription_status="past_due"
    )

    logger.warning(
        "payment_failed",
        api_key_id=api_key_id,
        email=customer_email,
        subscription_id=subscription_id,
        reason=reason
    )

    return True, api_key_id
