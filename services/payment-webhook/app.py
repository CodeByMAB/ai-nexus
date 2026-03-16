"""
Payment Webhook Service - Main Application
FastAPI app handling Stripe, Strike, and BTCPay webhooks
"""
from fastapi import FastAPI, Request, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from typing import Dict, Optional
import structlog
from datetime import datetime
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import stripe
import uuid

from config import settings
from handlers.stripe_handler import process_stripe_webhook
from handlers.strike_handler import process_strike_webhook, create_strike_invoice
from services.promo_code_validator import get_promo_details, validate_promo_code
from services.token_manager import get_available_tokens, get_token_usage_stats
from utils.database import get_api_key_by_email, ensure_event_deduplication_table, create_invoice, get_invoice_by_id
from utils.security import (
    validate_email,
    validate_plan_tier,
    validate_pack_type,
    validate_payment_method,
    validate_promo_code,
    validate_amount,
    sanitize_email,
    sanitize_string,
    sanitize_html
)
from utils.auth import verify_api_key
from utils.secure_logging import SecureLogProcessor
from middleware.security_headers import SecurityHeadersMiddleware

# Configure structured logging with automatic credential redaction
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        SecureLogProcessor(),  # Automatic credential redaction
        structlog.processors.JSONRenderer() if settings.enable_structured_logging else structlog.dev.ConsoleRenderer()
    ]
)

logger = structlog.get_logger()

# Configure Stripe
stripe.api_key = settings.stripe_secret_key
stripe.api_version = settings.stripe_api_version

# Initialize rate limiter
limiter = Limiter(key_func=get_remote_address)

# Initialize FastAPI app
app = FastAPI(
    title="Playground AI Payment Webhook Service",
    description="Handles payment webhooks and API key provisioning for Stripe, Strike, and BTCPay",
    version="1.0.0",
    docs_url="/docs" if settings.is_development else None,  # Disable in production
    redoc_url="/redoc" if settings.is_development else None
)

# Add rate limiter to app state
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS middleware - Restrictive configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],  # Only allowed methods
    allow_headers=["Content-Type", "Authorization", "Accept", "X-Requested-With", "X-API-Key"],  # Whitelist headers
    max_age=600  # Cache preflight for 10 minutes
)

# Security headers middleware
app.add_middleware(
    SecurityHeadersMiddleware,
    hsts_max_age=31536000,  # 1 year in seconds
    environment=settings.environment
)


# ========================================
# Startup/Shutdown Events
# ========================================

@app.on_event("startup")
async def startup_event():
    """Run on application startup"""
    # Initialize event deduplication table
    await ensure_event_deduplication_table()

    logger.info(
        "payment_webhook_service_starting",
        environment=settings.environment,
        port=settings.port,
        stripe_configured=bool(settings.stripe_secret_key),
        strike_configured=bool(settings.strike_api_key),
        btcpay_enabled=settings.btcpay_enabled
    )


@app.on_event("shutdown")
async def shutdown_event():
    """Run on application shutdown"""
    logger.info("payment_webhook_service_shutting_down")


# ========================================
# Webhook Endpoints
# ========================================

@app.post("/webhooks/stripe")
@limiter.limit("10/minute")  # Strict limit - webhooks should only come from Stripe
async def stripe_webhook(request: Request):
    """
    Stripe webhook endpoint
    Handles all Stripe payment events

    Rate limit: 10 requests/minute per IP
    """
    try:
        result = await process_stripe_webhook(request)
        return JSONResponse(content=result, status_code=200)

    except HTTPException as e:
        logger.error("stripe_webhook_error", status_code=e.status_code, detail=e.detail)
        raise

    except Exception as e:
        logger.error("stripe_webhook_exception", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/webhooks/strike")
@limiter.limit("10/minute")  # Strict limit - webhooks should only come from Strike
async def strike_webhook(request: Request):
    """
    Strike webhook endpoint
    Handles Strike payment events (both USD and BTC/Lightning)

    Rate limit: 10 requests/minute per IP
    """
    try:
        result = await process_strike_webhook(request)
        return JSONResponse(content=result, status_code=200)

    except HTTPException as e:
        logger.error("strike_webhook_error", status_code=e.status_code, detail=e.detail)
        raise

    except Exception as e:
        logger.error("strike_webhook_exception", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/webhooks/btcpay")
@limiter.limit("10/minute")  # Strict limit - webhooks should only come from BTCPay
async def btcpay_webhook(request: Request):
    """
    BTCPay webhook endpoint
    Handles BTCPay Server payment events (TOR-based, ultra-privacy)

    Rate limit: 10 requests/minute per IP
    """
    # TODO: Implement BTCPay handler when ready
    logger.info("btcpay_webhook_received")
    return {"status": "btcpay_not_implemented_yet"}


# ========================================
# API Endpoints for Sign-up Website
# ========================================

@app.post("/api/create-payment")
@limiter.limit("20/minute")  # Allow reasonable payment creation attempts
async def create_payment(request: Request):
    """
    Create a payment intent/invoice
    Called by sign-up website to initiate payment

    Rate limit: 20 requests/minute per IP

    Request body:
    {
        "payment_method": "stripe" | "strike_usd" | "strike_btc" | "btcpay",
        "payment_type": "subscription" | "token_pack",
        "email": "user@example.com",
        "plan_tier": "family" | "regular" | "ultra_privacy" | "trial" | "beta",
        "pack_type": "trial" | "small" | "medium" | "large",  // if token_pack
        "promo_code": "FAMBAM2025",  // optional
        "pgp_public_key": "-----BEGIN PGP PUBLIC KEY BLOCK-----...",  // optional
        "return_url": "https://YOURDOMAIN.COM/success"
    }
    """
    try:
        data = await request.json()

        # Extract and sanitize inputs
        payment_method = sanitize_string(data.get('payment_method', 'stripe'), 50)
        payment_type = sanitize_string(data.get('payment_type', 'subscription'), 50)
        email = data.get('email')
        plan_tier = sanitize_string(data.get('plan_tier', 'regular'), 50)
        pack_type = sanitize_string(data.get('pack_type', ''), 50) if data.get('pack_type') else None
        promo_code = sanitize_string(data.get('promo_code', ''), 50) if data.get('promo_code') else None
        return_url = sanitize_string(data.get('return_url', settings.signup_website_url), 500)

        # Validate email
        if not email:
            raise HTTPException(status_code=400, detail="Email is required")

        valid, error = validate_email(email)
        if not valid:
            raise HTTPException(status_code=400, detail=f"Invalid email: {error}")

        email = sanitize_email(email)

        # Validate payment method
        valid, error = validate_payment_method(payment_method)
        if not valid:
            raise HTTPException(status_code=400, detail=error)

        # Validate payment type
        if payment_type not in ['subscription', 'token_pack']:
            raise HTTPException(status_code=400, detail="Invalid payment_type. Must be 'subscription' or 'token_pack'")

        # Validate plan tier for subscriptions
        if payment_type == 'subscription':
            valid, error = validate_plan_tier(plan_tier)
            if not valid:
                raise HTTPException(status_code=400, detail=error)

        # Validate pack type for token packs
        if payment_type == 'token_pack':
            if not pack_type:
                raise HTTPException(status_code=400, detail="pack_type is required for token pack purchases")
            valid, error = validate_pack_type(pack_type)
            if not valid:
                raise HTTPException(status_code=400, detail=error)

        # Validate promo code format if provided
        if promo_code:
            valid, error = validate_promo_code(promo_code)
            if not valid:
                logger.warning("invalid_promo_code_format", code=promo_code, error=error)
                # Don't fail, just ignore invalid promo
                promo_code = None

        # Validate promo code if provided
        final_price = settings.get_plan_price(plan_tier)
        if promo_code and payment_type == 'subscription':
            valid, promo, error = await validate_promo_code(promo_code, plan_tier)
            if not valid:
                raise HTTPException(status_code=400, detail=error)
            # Apply discount
            discount = promo.get('discount_amount', 0) or 0
            final_price = max(0, final_price - discount)

        # Generate unique invoice ID for tracking
        invoice_id = f"inv_{uuid.uuid4().hex[:16]}"

        # Determine amount
        amount = final_price if payment_type == 'subscription' else settings.get_pack_details(pack_type)['price']

        # Create invoice record in database
        await create_invoice(
            invoice_id=invoice_id,
            customer_email=email,
            amount=amount,
            currency="USD",
            payment_type=payment_type,
            payment_method=payment_method,
            plan_tier=plan_tier if payment_type == 'subscription' else None,
            pack_type=pack_type if payment_type == 'token_pack' else None,
            promo_code=promo_code
        )

        # Create payment based on method
        if payment_method == 'stripe':
            # Create Stripe PaymentIntent for embedded payment
            amount_cents = int(amount * 100)  # Convert to cents

            try:
                payment_intent = stripe.PaymentIntent.create(
                    amount=amount_cents,
                    currency="usd",
                    payment_method_types=["card"],
                    metadata={
                        "invoice_id": invoice_id,
                        "email": email,
                        "plan_tier": plan_tier if payment_type == 'subscription' else '',
                        "pack_type": pack_type if payment_type == 'token_pack' else '',
                        "payment_type": payment_type,
                        "promo_code": promo_code or ''
                    }
                )

                logger.info(
                    "stripe_payment_intent_created",
                    invoice_id=invoice_id,
                    payment_intent_id=payment_intent.id,
                    amount=amount,
                    email=email
                )

                return {
                    "payment_method": "stripe",
                    "mode": "embedded",
                    "client_secret": payment_intent.client_secret,
                    "publishable_key": settings.stripe_publishable_key,
                    "amount": amount_cents,
                    "currency": "usd",
                    "invoice_id": invoice_id,
                    "payment_intent_id": payment_intent.id
                }

            except stripe.error.StripeError as e:
                logger.error("stripe_payment_intent_error", error=str(e), invoice_id=invoice_id)
                raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")

        elif payment_method == 'strike_usd' or payment_method == 'strike_btc':
            # Create Strike invoice for embedded payment
            description = f"{plan_tier} plan" if payment_type == 'subscription' else f"{pack_type} token pack"

            invoice = await create_strike_invoice(
                customer_email=email,
                amount=amount,
                currency='USD',
                plan_tier=plan_tier,
                payment_type=payment_type,
                pack_type=pack_type,
                description=description
            )

            if invoice:
                strike_invoice_id = invoice.get('invoiceId')
                logger.info(
                    "strike_invoice_created",
                    invoice_id=invoice_id,
                    strike_invoice_id=strike_invoice_id,
                    amount=amount,
                    email=email
                )

                return {
                    "payment_method": payment_method,
                    "mode": "embedded",
                    "invoice_id": invoice_id,
                    "strike_invoice_id": strike_invoice_id,
                    "payment_url": f"https://strike.me/pay/{strike_invoice_id}",
                    "lightning_invoice": invoice.get('lnInvoice'),
                    "amount": str(amount),
                    "currency": "USD"
                }
            else:
                raise HTTPException(status_code=500, detail="Failed to create Strike invoice")

        elif payment_method == 'btcpay':
            # Create BTCPay invoice for embedded payment
            # TODO: Implement BTCPay invoice creation when BTCPay is configured
            if not settings.btcpay_enabled:
                raise HTTPException(status_code=400, detail="BTCPay is not enabled")

            logger.info(
                "btcpay_invoice_requested",
                invoice_id=invoice_id,
                amount=amount,
                email=email
            )

            # Placeholder for BTCPay implementation
            return {
                "payment_method": "btcpay",
                "mode": "embedded",
                "invoice_id": invoice_id,
                "message": "BTCPay integration coming soon",
                "amount": str(amount),
                "currency": "USD"
            }

        else:
            raise HTTPException(status_code=400, detail=f"Unsupported payment method: {payment_method}")

    except HTTPException:
        raise
    except Exception as e:
        logger.error("create_payment_error", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create payment")


@app.get("/api/payment-status/{invoice_id}")
@limiter.limit("60/minute")  # Frontend polls for status
async def get_payment_status(invoice_id: str, request: Request):
    """
    Check payment status by invoice ID
    Used by frontend to poll for payment completion

    Rate limit: 60 requests/minute per IP

    Returns:
    - status: "pending", "paid", "confirmed", "failed", "expired"
    - invoice_id: Invoice identifier
    - amount: Payment amount (optional)
    - created_at: Invoice creation timestamp (optional)
    """
    try:
        # Sanitize invoice_id
        invoice_id = sanitize_string(invoice_id, 255)

        # Query database for invoice
        invoice = await get_invoice_by_id(invoice_id)

        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")

        # Return invoice status
        return {
            "status": invoice.get('status', 'pending'),
            "invoice_id": invoice_id,
            "amount": invoice.get('amount'),
            "currency": invoice.get('currency'),
            "payment_method": invoice.get('payment_method'),
            "created_at": invoice.get('created_at'),
            "completed_at": invoice.get('completed_at')
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("payment_status_error", invoice_id=invoice_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to get payment status")


@app.get("/api/promo/{code}")
@limiter.limit("60/minute")  # Users might check multiple promo codes
async def check_promo_code(code: str, request: Request):
    """
    Check if a promo code is valid
    Returns promo details including discount information

    Rate limit: 60 requests/minute per IP
    """
    try:
        promo_details = await get_promo_details(code.upper())

        if not promo_details:
            raise HTTPException(status_code=404, detail="Promo code not found")

        return promo_details

    except HTTPException:
        raise
    except Exception as e:
        logger.error("promo_check_error", code=code, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to check promo code")


@app.get("/api/subscription/status/{email}")
@limiter.limit("30/minute")  # Moderate limit for status checks
async def get_subscription_status(
    email: str,
    request: Request,
    api_key: str = Depends(verify_api_key)
):
    """
    Get subscription status and token balance for a user

    **Authentication Required:** Admin API key via X-API-Key header or Authorization: Bearer token

    Rate limit: 30 requests/minute per IP

    Security:
    - Requires admin API key for access
    - Email format validated
    - Output sanitized against XSS
    """
    try:
        # Validate email
        valid, error = validate_email(email)
        if not valid:
            raise HTTPException(status_code=400, detail="Invalid email format")

        email = sanitize_email(email)

        api_key = await get_api_key_by_email(email)

        if not api_key:
            raise HTTPException(status_code=404, detail="Subscription not found")

        # Get token stats
        stats = await get_token_usage_stats(api_key['id'])

        # Sanitize output data to prevent XSS if displayed in a web interface
        return {
            "email": sanitize_html(email),
            "active": bool(api_key.get('is_active')),
            "plan_tier": sanitize_html(api_key.get('plan_tier', '')),
            "monthly_price": api_key.get('monthly_price'),
            "payment_method": sanitize_html(api_key.get('payment_method', '')),
            "subscription_status": sanitize_html(api_key.get('subscription_status', '')),
            "tokens": stats,
            "created_at": api_key.get('created_at'),
            "expires_at": api_key.get('expires_at')
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("subscription_status_error", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to get subscription status")


# ========================================
# Health & Info Endpoints
# ========================================

@app.get("/health")
@limiter.limit("100/minute")  # High limit for monitoring systems
async def health_check(request: Request):
    """
    Health check endpoint

    Rate limit: 100 requests/minute per IP
    """
    return {
        "status": "healthy",
        "service": "payment-webhook",
        "version": "1.0.0",
        "timestamp": datetime.now().isoformat(),
        "environment": settings.environment
    }


@app.get("/")
@limiter.limit("100/minute")  # High limit for exploratory requests
async def root(request: Request):
    """
    Root endpoint - Service information

    Rate limit: 100 requests/minute per IP
    """
    return {
        "service": "Playground AI Payment Webhook Service",
        "version": "1.0.0",
        "docs": "/docs" if settings.is_development else "disabled",
        "endpoints": {
            "webhooks": {
                "stripe": "/webhooks/stripe",
                "strike": "/webhooks/strike",
                "btcpay": "/webhooks/btcpay"
            },
            "api": {
                "create_payment": "/api/create-payment",
                "promo_code": "/api/promo/{code}",
                "subscription_status": "/api/subscription/status/{email}"
            },
            "health": "/health"
        }
    }


# ========================================
# Error Handlers
# ========================================

@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    """Handle 404 errors - Don't expose internal paths in production"""
    logger.warning("endpoint_not_found", path=request.url.path, method=request.method)

    if settings.is_production:
        # Don't expose path in production
        return JSONResponse(
            status_code=404,
            content={"error": "Not found"}
        )
    else:
        # Show path in development for debugging
        return JSONResponse(
            status_code=404,
            content={"error": "Not found", "path": request.url.path}
        )


@app.exception_handler(500)
async def internal_error_handler(request: Request, exc):
    """Handle 500 errors - Don't leak internal details in production"""
    logger.error("internal_server_error", path=request.url.path, error=str(exc))

    if settings.is_production:
        # Generic error message in production
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error"}
        )
    else:
        # More details in development
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error", "detail": str(exc)[:200]}
        )


# ========================================
# Run Application
# ========================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        reload=settings.is_development
    )
