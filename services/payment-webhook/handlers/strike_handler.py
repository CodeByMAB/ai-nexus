"""
Strike Webhook Handler
Processes Strike payment events (USD and BTC/Lightning)
All payments settle in BTC for the merchant
"""
import hmac
import hashlib
import json
from typing import Dict, Optional, Tuple
from fastapi import Request, HTTPException
import httpx
import structlog

from config import settings
from services.subscription_manager import (
    handle_new_subscription,
    handle_subscription_renewal,
    handle_token_pack_purchase
)
from utils.database import is_event_already_processed, mark_event_as_processed

logger = structlog.get_logger()


def verify_strike_signature(payload: bytes, signature: str) -> bool:
    """
    Verify Strike webhook signature using HMAC SHA256

    Strike signs webhooks with: HMAC-SHA256(webhook_secret, payload)
    """
    if not settings.strike_webhook_secret:
        logger.warning("strike_webhook_secret_not_configured")
        return True  # Allow in development if not configured

    expected_signature = hmac.new(
        settings.strike_webhook_secret.encode('utf-8'),
        payload,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(signature, expected_signature)


async def get_strike_invoice_details(invoice_id: str) -> Optional[Dict]:
    """
    Fetch invoice details from Strike API
    """
    headers = {
        "Authorization": f"Bearer {settings.strike_api_key}",
        "Accept": "application/json"
    }

    url = f"{settings.strike_api_base_url}/v1/invoices/{invoice_id}"

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers)

            if response.status_code == 200:
                return response.json()
            else:
                logger.error(
                    "strike_invoice_fetch_failed",
                    invoice_id=invoice_id,
                    status_code=response.status_code
                )
                return None

    except Exception as e:
        logger.error("strike_api_error", error=str(e), invoice_id=invoice_id)
        return None


async def handle_invoice_updated(event: Dict) -> Tuple[bool, Optional[str]]:
    """
    Handle invoice.updated event from Strike
    This is triggered when an invoice state changes (especially to PAID)
    """
    entity_id = event.get('entityId')  # This is the invoice ID
    changes = event.get('changes', [])

    if 'state' not in changes:
        logger.info("strike_invoice_updated_no_state_change", invoice_id=entity_id)
        return True, "No state change"

    # Fetch full invoice details
    invoice = await get_strike_invoice_details(entity_id)

    if not invoice:
        logger.error("strike_invoice_not_found", invoice_id=entity_id)
        return False, "Invoice not found"

    state = invoice.get('state')
    logger.info(
        "strike_invoice_state_updated",
        invoice_id=entity_id,
        state=state
    )

    # Only process PAID invoices
    if state != 'PAID':
        logger.info(
            "strike_invoice_not_paid",
            invoice_id=entity_id,
            state=state
        )
        return True, f"Invoice state: {state}"

    # Get invoice details
    amount_obj = invoice.get('amount', {})
    amount = float(amount_obj.get('amount', 0))
    currency = amount_obj.get('currency', 'USD')

    # Get customer info from correlationId (we should store email there)
    correlation_id = invoice.get('correlationId', '')
    description = invoice.get('description', '')

    # Parse metadata from correlationId or description
    # Format: "email|plan_tier|payment_type|pack_type"
    try:
        parts = correlation_id.split('|') if correlation_id else []
        customer_email = parts[0] if len(parts) > 0 else None
        plan_tier = parts[1] if len(parts) > 1 else 'regular'
        payment_type = parts[2] if len(parts) > 2 else 'subscription'
        pack_type = parts[3] if len(parts) > 3 else None
    except Exception as e:
        logger.error("strike_metadata_parse_error", error=str(e), correlation_id=correlation_id)
        return False, "Failed to parse metadata"

    if not customer_email:
        logger.error("strike_missing_customer_email", invoice_id=entity_id)
        return False, "Missing customer email"

    # Determine payment method (USD or BTC)
    # Strike invoices can be paid with either USD or BTC/Lightning
    # We check the payment method from the invoice
    payment_method = 'strike_btc'  # Default to BTC

    # Check if payment was made with USD or BTC
    # (Strike API might have this info, adjust as needed)
    # For now, we'll use 'strike_btc' for all since it settles in BTC

    logger.info(
        "strike_processing_payment",
        invoice_id=entity_id,
        email=customer_email,
        amount=amount,
        currency=currency,
        payment_type=payment_type,
        plan_tier=plan_tier
    )

    if payment_type == 'subscription':
        # Handle new subscription
        # Note: Strike doesn't have native subscriptions like Stripe
        # This is a one-time payment that grants subscription access
        # We use invoice_id as the "subscription_id" for tracking

        success, api_key, api_key_id, payment_event_id = await handle_new_subscription(
            customer_email=customer_email,
            plan_tier=plan_tier,
            payment_method=payment_method,
            amount=amount,
            currency=currency,
            subscription_id=entity_id,  # Use invoice ID as subscription ID
            customer_id=invoice.get('correlationId'),  # Use correlation as customer ID
            provider_payment_id=entity_id,
            event_id=event.get('id')
        )

        if success:
            logger.info(
                "strike_subscription_created",
                api_key_id=api_key_id,
                invoice_id=entity_id,
                email=customer_email
            )
            return True, api_key
        else:
            logger.error("strike_subscription_failed", email=customer_email)
            return False, None

    elif payment_type == 'token_pack':
        # Handle token pack purchase
        if not pack_type:
            logger.error("strike_missing_pack_type", invoice_id=entity_id)
            return False, "Missing pack_type"

        success, api_key_id, tokens_added = await handle_token_pack_purchase(
            customer_email=customer_email,
            pack_type=pack_type,
            payment_method=payment_method,
            amount=amount,
            currency=currency,
            provider_payment_id=entity_id,
            event_id=event.get('id')
        )

        if success:
            logger.info(
                "strike_token_pack_purchased",
                api_key_id=api_key_id,
                pack_type=pack_type,
                tokens_added=tokens_added,
                email=customer_email
            )
            return True, None
        else:
            logger.error("strike_token_pack_failed", email=customer_email)
            return False, None

    else:
        logger.warning("strike_unknown_payment_type", payment_type=payment_type)
        return False, f"Unknown payment type: {payment_type}"


async def process_strike_webhook(request: Request) -> Dict:
    """
    Main Strike webhook processor
    """
    # Get payload and signature
    payload = await request.body()
    signature = request.headers.get('strike-signature', '')

    # Verify signature
    if not verify_strike_signature(payload, signature):
        logger.error("strike_invalid_signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    # Parse event
    try:
        event = json.loads(payload)
    except json.JSONDecodeError as e:
        logger.error("strike_invalid_json", error=str(e))
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = event.get('eventType')
    event_id = event.get('id', 'unknown')
    entity_id = event.get('entityId')

    logger.info(
        "strike_event_received",
        event_type=event_type,
        event_id=event_id,
        entity_id=entity_id
    )

    # Check for duplicate/replay events
    if await is_event_already_processed(event_id, 'strike'):
        logger.warning("strike_duplicate_event_ignored", event_id=event_id, event_type=event_type)
        return {
            "status": "ignored",
            "event_type": event_type,
            "event_id": event_id,
            "message": "Duplicate event already processed"
        }

    # Route to handler
    if event_type == 'invoice.updated':
        try:
            success, message = await handle_invoice_updated(event)

            if success:
                # Mark event as processed to prevent replay
                await mark_event_as_processed(event_id, event_type, 'strike')

                logger.info(
                    "strike_event_processed",
                    event_type=event_type,
                    entity_id=entity_id,
                    result=message
                )
                return {
                    "status": "success",
                    "event_type": event_type,
                    "entity_id": entity_id,
                    "message": message
                }
            else:
                logger.error(
                    "strike_event_failed",
                    event_type=event_type,
                    entity_id=entity_id,
                    error=message
                )
                return {
                    "status": "failed",
                    "event_type": event_type,
                    "entity_id": entity_id,
                    "error": message
                }

        except Exception as e:
            logger.error(
                "strike_handler_exception",
                event_type=event_type,
                error=str(e),
                exc_info=True
            )
            raise HTTPException(status_code=500, detail=f"Handler error: {str(e)}")

    else:
        # Unhandled event type
        logger.info(
            "strike_event_unhandled",
            event_type=event_type,
            entity_id=entity_id
        )
        return {
            "status": "ignored",
            "event_type": event_type,
            "entity_id": entity_id,
            "message": "Event type not handled"
        }


async def create_strike_invoice(
    customer_email: str,
    amount: float,
    currency: str,
    plan_tier: str,
    payment_type: str = 'subscription',
    pack_type: Optional[str] = None,
    description: Optional[str] = None
) -> Optional[Dict]:
    """
    Create a Strike invoice for payment

    Returns invoice details including Lightning invoice and payment URL
    """
    headers = {
        "Authorization": f"Bearer {settings.strike_api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    # Build correlation ID with metadata
    # Format: "email|plan_tier|payment_type|pack_type"
    correlation_parts = [customer_email, plan_tier, payment_type]
    if pack_type:
        correlation_parts.append(pack_type)
    correlation_id = '|'.join(correlation_parts)

    # Build description
    if not description:
        if payment_type == 'subscription':
            description = f"{plan_tier.replace('_', ' ').title()} Plan - {customer_email}"
        else:
            description = f"{pack_type.title()} Token Pack - {customer_email}"

    # Create invoice payload
    payload = {
        "amount": {
            "amount": str(amount),
            "currency": currency
        },
        "description": description,
        "correlationId": correlation_id
    }

    url = f"{settings.strike_api_base_url}/v1/invoices"

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=headers, json=payload)

            if response.status_code in [200, 201]:
                invoice = response.json()
                logger.info(
                    "strike_invoice_created",
                    invoice_id=invoice.get('invoiceId'),
                    email=customer_email,
                    amount=amount
                )
                return invoice
            else:
                logger.error(
                    "strike_invoice_creation_failed",
                    status_code=response.status_code,
                    response=response.text
                )
                return None

    except Exception as e:
        logger.error("strike_api_create_error", error=str(e))
        return None
