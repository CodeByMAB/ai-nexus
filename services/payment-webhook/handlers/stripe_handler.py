"""
Stripe Webhook Handler
Processes Stripe payment events and provisions API keys
"""
import stripe
import json
from typing import Dict, Optional, Tuple
from fastapi import Request, HTTPException
import structlog

from config import settings
from services.subscription_manager import (
    handle_new_subscription,
    handle_subscription_renewal,
    handle_subscription_canceled,
    handle_token_pack_purchase,
    handle_payment_failed
)
from utils.database import (
    is_event_already_processed,
    mark_event_as_processed,
    update_invoice_status
)

logger = structlog.get_logger()

# Configure Stripe
stripe.api_key = settings.stripe_secret_key
stripe.api_version = settings.stripe_api_version


async def verify_stripe_signature(request: Request) -> Dict:
    """
    Verify Stripe webhook signature and return the event

    Raises:
        HTTPException: If signature verification fails
    """
    payload = await request.body()
    sig_header = request.headers.get('stripe-signature')

    if not sig_header:
        logger.error("stripe_webhook_missing_signature")
        raise HTTPException(status_code=400, detail="Missing signature header")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret
        )
        logger.info("stripe_webhook_verified", event_type=event['type'], event_id=event['id'])
        return event
    except ValueError as e:
        logger.error("stripe_webhook_invalid_payload", error=str(e))
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError as e:
        logger.error("stripe_webhook_invalid_signature", error=str(e))
        raise HTTPException(status_code=400, detail="Invalid signature")


async def handle_checkout_session_completed(event: Dict) -> Tuple[bool, Optional[str]]:
    """
    Handle checkout.session.completed event
    This is triggered when a customer completes a Stripe Checkout session
    Can be either a subscription or one-time payment for token packs
    """
    session = event['data']['object']

    customer_email = session.get('customer_email') or session.get('customer_details', {}).get('email')
    customer_id = session.get('customer')
    payment_intent = session.get('payment_intent')
    subscription_id = session.get('subscription')
    mode = session.get('mode')  # 'subscription' or 'payment'

    # Get metadata
    metadata = session.get('metadata', {})
    plan_tier = metadata.get('plan_tier', 'regular')
    promo_code = metadata.get('promo_code')
    payment_type = metadata.get('payment_type', 'subscription')  # 'subscription' or 'token_pack'
    pack_type = metadata.get('pack_type')

    logger.info(
        "stripe_checkout_completed",
        mode=mode,
        payment_type=payment_type,
        customer_email=customer_email,
        plan_tier=plan_tier,
        subscription_id=subscription_id
    )

    if mode == 'subscription' or payment_type == 'subscription':
        # Handle new subscription
        if not subscription_id:
            logger.error("stripe_missing_subscription_id", session_id=session['id'])
            return False, "Missing subscription ID"

        # Get subscription details for amount
        subscription = stripe.Subscription.retrieve(subscription_id)
        amount = subscription['items']['data'][0]['price']['unit_amount'] / 100  # Convert from cents
        currency = subscription['items']['data'][0]['price']['currency'].upper()

        success, api_key, api_key_id, payment_event_id = await handle_new_subscription(
            customer_email=customer_email,
            plan_tier=plan_tier,
            payment_method='stripe',
            amount=amount,
            currency=currency,
            subscription_id=subscription_id,
            customer_id=customer_id,
            promo_code=promo_code,
            provider_payment_id=payment_intent,
            event_id=event['id']
        )

        if success:
            logger.info(
                "stripe_subscription_created",
                api_key_id=api_key_id,
                subscription_id=subscription_id,
                email=customer_email
            )
            return True, api_key
        else:
            logger.error("stripe_subscription_creation_failed", email=customer_email)
            return False, None

    elif mode == 'payment' or payment_type == 'token_pack':
        # Handle one-time token pack purchase
        if not pack_type:
            logger.error("stripe_missing_pack_type", session_id=session['id'])
            return False, "Missing pack_type in metadata"

        amount = session.get('amount_total', 0) / 100  # Convert from cents
        currency = session.get('currency', 'usd').upper()

        success, api_key_id, tokens_added = await handle_token_pack_purchase(
            customer_email=customer_email,
            pack_type=pack_type,
            payment_method='stripe',
            amount=amount,
            currency=currency,
            provider_payment_id=payment_intent,
            event_id=event['id']
        )

        if success:
            logger.info(
                "stripe_token_pack_purchased",
                api_key_id=api_key_id,
                pack_type=pack_type,
                tokens_added=tokens_added,
                email=customer_email
            )
            return True, None
        else:
            logger.error("stripe_token_pack_failed", email=customer_email, pack_type=pack_type)
            return False, None

    else:
        logger.warning("stripe_unknown_checkout_mode", mode=mode, session_id=session['id'])
        return False, f"Unknown mode: {mode}"


async def handle_invoice_payment_succeeded(event: Dict) -> Tuple[bool, str]:
    """
    Handle invoice.payment_succeeded event
    This is triggered for subscription renewals
    """
    invoice = event['data']['object']

    subscription_id = invoice.get('subscription')
    customer_email = invoice.get('customer_email')
    amount = invoice.get('amount_paid', 0) / 100  # Convert from cents
    currency = invoice.get('currency', 'usd').upper()
    payment_intent = invoice.get('payment_intent')
    billing_reason = invoice.get('billing_reason')

    logger.info(
        "stripe_invoice_paid",
        subscription_id=subscription_id,
        billing_reason=billing_reason,
        amount=amount
    )

    # Skip the first invoice (it's handled by checkout.session.completed)
    if billing_reason == 'subscription_create':
        logger.info("stripe_initial_invoice_skip", subscription_id=subscription_id)
        return True, "Initial invoice, handled by checkout"

    # Handle renewal
    if subscription_id:
        success, api_key_id = await handle_subscription_renewal(
            subscription_id=subscription_id,
            payment_method='stripe',
            amount=amount,
            currency=currency,
            provider_payment_id=payment_intent,
            event_id=event['id']
        )

        if success:
            logger.info(
                "stripe_subscription_renewed",
                subscription_id=subscription_id,
                api_key_id=api_key_id
            )
            return True, "Subscription renewed"
        else:
            logger.error("stripe_renewal_failed", subscription_id=subscription_id)
            return False, "Renewal failed"

    return False, "No subscription ID"


async def handle_customer_subscription_deleted(event: Dict) -> Tuple[bool, str]:
    """
    Handle customer.subscription.deleted event
    This is triggered when a subscription is canceled
    """
    subscription = event['data']['object']
    subscription_id = subscription['id']
    cancel_at = subscription.get('canceled_at')
    cancellation_details = subscription.get('cancellation_details', {})
    reason = cancellation_details.get('reason', 'customer_canceled')

    logger.info(
        "stripe_subscription_deleted",
        subscription_id=subscription_id,
        reason=reason
    )

    success, api_key_id = await handle_subscription_canceled(
        subscription_id=subscription_id,
        payment_method='stripe',
        reason=reason
    )

    if success:
        logger.info(
            "stripe_subscription_canceled",
            subscription_id=subscription_id,
            api_key_id=api_key_id
        )
        return True, "Subscription canceled"
    else:
        logger.error("stripe_cancellation_failed", subscription_id=subscription_id)
        return False, "Cancellation handling failed"


async def handle_invoice_payment_failed(event: Dict) -> Tuple[bool, str]:
    """
    Handle invoice.payment_failed event
    This is triggered when a subscription payment fails
    """
    invoice = event['data']['object']
    subscription_id = invoice.get('subscription')
    customer_email = invoice.get('customer_email')
    amount = invoice.get('amount_due', 0) / 100
    attempt_count = invoice.get('attempt_count', 0)

    logger.warning(
        "stripe_payment_failed",
        subscription_id=subscription_id,
        email=customer_email,
        amount=amount,
        attempt_count=attempt_count
    )

    if subscription_id:
        success, api_key_id = await handle_payment_failed(
            subscription_id=subscription_id,
            payment_method='stripe',
            reason=f"payment_failed_attempt_{attempt_count}"
        )

        if success:
            logger.info(
                "stripe_payment_failure_handled",
                subscription_id=subscription_id,
                api_key_id=api_key_id
            )
            return True, "Payment failure recorded"
        else:
            return False, "Failed to handle payment failure"

    return False, "No subscription ID"


async def handle_payment_intent_succeeded(event: Dict) -> Tuple[bool, Optional[str]]:
    """
    Handle payment_intent.succeeded event
    This is triggered for embedded payment flows (Stripe Elements/PaymentIntent API)
    """
    payment_intent = event['data']['object']

    # Get metadata from payment intent
    metadata = payment_intent.get('metadata', {})
    invoice_id = metadata.get('invoice_id')
    customer_email = metadata.get('email')
    payment_type = metadata.get('payment_type')
    plan_tier = metadata.get('plan_tier')
    pack_type = metadata.get('pack_type')
    promo_code = metadata.get('promo_code')

    # Get payment details
    amount = payment_intent.get('amount', 0) / 100  # Convert from cents
    currency = payment_intent.get('currency', 'usd').upper()
    payment_intent_id = payment_intent['id']

    logger.info(
        "stripe_payment_intent_succeeded",
        payment_type=payment_type,
        customer_email=customer_email,
        invoice_id=invoice_id,
        amount=amount,
        payment_intent_id=payment_intent_id
    )

    if not customer_email:
        logger.error("stripe_payment_intent_missing_email", payment_intent_id=payment_intent_id)
        return False, "Missing customer email in metadata"

    if payment_type == 'subscription':
        # Handle new subscription via embedded payment
        if not plan_tier:
            logger.error("stripe_payment_intent_missing_plan_tier", payment_intent_id=payment_intent_id)
            return False, "Missing plan_tier in metadata"

        success, api_key, api_key_id, payment_event_id = await handle_new_subscription(
            customer_email=customer_email,
            plan_tier=plan_tier,
            payment_method='stripe',
            amount=amount,
            currency=currency,
            subscription_id=None,  # Embedded payments don't have subscription_id initially
            customer_id=payment_intent.get('customer'),
            promo_code=promo_code,
            provider_payment_id=payment_intent_id,
            event_id=event['id']
        )

        if success:
            # Update invoice status to paid
            if invoice_id:
                await update_invoice_status(
                    invoice_id=invoice_id,
                    status='paid',
                    completed_at=payment_intent.get('created'),
                    provider_payment_id=payment_intent_id
                )

            logger.info(
                "stripe_embedded_subscription_created",
                api_key_id=api_key_id,
                email=customer_email,
                invoice_id=invoice_id
            )
            return True, api_key
        else:
            logger.error("stripe_embedded_subscription_failed", email=customer_email)
            return False, "Subscription creation failed"

    elif payment_type == 'token_pack':
        # Handle token pack purchase via embedded payment
        if not pack_type:
            logger.error("stripe_payment_intent_missing_pack_type", payment_intent_id=payment_intent_id)
            return False, "Missing pack_type in metadata"

        success, api_key_id, tokens_added = await handle_token_pack_purchase(
            customer_email=customer_email,
            pack_type=pack_type,
            payment_method='stripe',
            amount=amount,
            currency=currency,
            provider_payment_id=payment_intent_id,
            event_id=event['id']
        )

        if success:
            # Update invoice status to paid
            if invoice_id:
                await update_invoice_status(
                    invoice_id=invoice_id,
                    status='paid',
                    completed_at=payment_intent.get('created'),
                    provider_payment_id=payment_intent_id
                )

            logger.info(
                "stripe_embedded_token_pack_purchased",
                api_key_id=api_key_id,
                pack_type=pack_type,
                tokens_added=tokens_added,
                email=customer_email,
                invoice_id=invoice_id
            )
            return True, None
        else:
            logger.error("stripe_embedded_token_pack_failed", email=customer_email, pack_type=pack_type)
            return False, "Token pack purchase failed"

    else:
        logger.warning("stripe_payment_intent_unknown_type", payment_type=payment_type, payment_intent_id=payment_intent_id)
        return False, f"Unknown payment type: {payment_type}"


async def process_stripe_webhook(request: Request) -> Dict:
    """
    Main Stripe webhook processor
    Routes events to appropriate handlers
    """
    # Verify signature and get event
    event = await verify_stripe_signature(request)

    event_type = event['type']
    event_id = event['id']

    logger.info("stripe_event_received", event_type=event_type, event_id=event_id)

    # Check for duplicate/replay events
    if await is_event_already_processed(event_id, 'stripe'):
        logger.warning("stripe_duplicate_event_ignored", event_id=event_id, event_type=event_type)
        return {
            "status": "ignored",
            "event_type": event_type,
            "event_id": event_id,
            "message": "Duplicate event already processed"
        }

    # Route to appropriate handler
    handlers = {
        'checkout.session.completed': handle_checkout_session_completed,
        'payment_intent.succeeded': handle_payment_intent_succeeded,  # NEW: For embedded payments
        'invoice.payment_succeeded': handle_invoice_payment_succeeded,
        'customer.subscription.deleted': handle_customer_subscription_deleted,
        'invoice.payment_failed': handle_invoice_payment_failed,
    }

    handler = handlers.get(event_type)

    if handler:
        try:
            success, message = await handler(event)

            if success:
                # Mark event as processed to prevent replay
                await mark_event_as_processed(event_id, event_type, 'stripe')

                logger.info(
                    "stripe_event_processed",
                    event_type=event_type,
                    event_id=event_id,
                    result=message
                )
                return {
                    "status": "success",
                    "event_type": event_type,
                    "event_id": event_id,
                    "message": message
                }
            else:
                logger.error(
                    "stripe_event_failed",
                    event_type=event_type,
                    event_id=event_id,
                    error=message
                )
                return {
                    "status": "failed",
                    "event_type": event_type,
                    "event_id": event_id,
                    "error": message
                }

        except Exception as e:
            logger.error(
                "stripe_handler_exception",
                event_type=event_type,
                event_id=event_id,
                error=str(e),
                exc_info=True
            )
            raise HTTPException(status_code=500, detail=f"Handler error: {str(e)}")

    else:
        # Unhandled event type - log but don't fail
        logger.info(
            "stripe_event_unhandled",
            event_type=event_type,
            event_id=event_id
        )
        return {
            "status": "ignored",
            "event_type": event_type,
            "event_id": event_id,
            "message": "Event type not handled"
        }
