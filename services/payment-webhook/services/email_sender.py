"""
Email Sender Service
Handles email delivery via SendGrid or SMTP
Supports PGP encryption for ultra-privacy users
"""
from typing import Optional, List
import structlog
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Email, To, Content
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from config import settings
from utils.security import sanitize_html, validate_email, sanitize_email

logger = structlog.get_logger()


async def send_email(
    to_email: str,
    subject: str,
    html_content: str,
    plain_content: Optional[str] = None,
    from_email: Optional[str] = None,
    from_name: Optional[str] = None
) -> bool:
    """
    Send an email using configured provider

    Args:
        to_email: Recipient email address
        subject: Email subject
        html_content: HTML version of email
        plain_content: Plain text version (optional, will strip HTML if not provided)
        from_email: Sender email (uses config default if not provided)
        from_name: Sender name (uses config default if not provided)

    Returns:
        True if sent successfully
    """
    # Validate email to prevent header injection
    valid, error = validate_email(to_email)
    if not valid:
        logger.error("invalid_recipient_email", email=to_email, error=error)
        return False

    # Sanitize subject to prevent header injection
    subject = subject.replace('\n', '').replace('\r', '')[:200]

    if settings.mock_emails:
        logger.info(
            "email_mocked",
            to=to_email,
            subject=subject,
            from_email=from_email or settings.email_from
        )
        return True

    from_email = from_email or settings.email_from
    from_name = from_name or settings.email_from_name

    if settings.email_provider == 'sendgrid':
        return await send_via_sendgrid(
            to_email, subject, html_content, plain_content, from_email, from_name
        )
    elif settings.email_provider == 'smtp':
        return await send_via_smtp(
            to_email, subject, html_content, plain_content, from_email, from_name
        )
    else:
        logger.error("unsupported_email_provider", provider=settings.email_provider)
        return False


async def send_via_sendgrid(
    to_email: str,
    subject: str,
    html_content: str,
    plain_content: Optional[str],
    from_email: str,
    from_name: str
) -> bool:
    """Send email via SendGrid"""
    try:
        message = Mail(
            from_email=Email(from_email, from_name),
            to_emails=To(to_email),
            subject=subject,
            plain_text_content=Content("text/plain", plain_content or html_content),
            html_content=Content("text/html", html_content)
        )

        sg = SendGridAPIClient(settings.sendgrid_api_key)
        response = sg.send(message)

        logger.info(
            "email_sent_sendgrid",
            to=to_email,
            subject=subject,
            status_code=response.status_code
        )

        return response.status_code in [200, 201, 202]

    except Exception as e:
        logger.error(
            "sendgrid_send_failed",
            to=to_email,
            error=str(e)
        )
        return False


async def send_via_smtp(
    to_email: str,
    subject: str,
    html_content: str,
    plain_content: Optional[str],
    from_email: str,
    from_name: str
) -> bool:
    """Send email via SMTP"""
    try:
        # Create message
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = f"{from_name} <{from_email}>"
        msg['To'] = to_email

        # Attach parts
        if plain_content:
            msg.attach(MIMEText(plain_content, 'plain'))
        msg.attach(MIMEText(html_content, 'html'))

        # Send
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            if settings.smtp_tls:
                server.starttls()
            if settings.smtp_user and settings.smtp_password:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)

        logger.info(
            "email_sent_smtp",
            to=to_email,
            subject=subject
        )

        return True

    except Exception as e:
        logger.error(
            "smtp_send_failed",
            to=to_email,
            error=str(e)
        )
        return False


async def send_api_key_email(
    to_email: str,
    api_key: str,
    plan_tier: str,
    monthly_price: float,
    payment_method: str,
    pgp_encrypt: bool = False,
    pgp_public_key: Optional[str] = None
) -> bool:
    """
    Send API key to new subscriber

    Args:
        to_email: Customer email
        api_key: Generated API key
        plan_tier: Subscription plan tier
        monthly_price: Monthly subscription price
        payment_method: Payment method used
        pgp_encrypt: Whether to PGP encrypt the email
        pgp_public_key: Customer's PGP public key (if encrypting)
    """
    # Validate email
    valid, error = validate_email(to_email)
    if not valid:
        logger.error("invalid_email_for_api_key", email=to_email, error=error)
        return False

    to_email = sanitize_email(to_email)

    # Sanitize all user inputs for HTML (prevent XSS)
    safe_plan_tier = sanitize_html(plan_tier.replace('_', ' ').title())
    safe_api_key = sanitize_html(api_key)
    safe_payment_method = sanitize_html(payment_method.replace('_', ' ').title())
    safe_api_key_preview = sanitize_html(api_key[:20] + "...")

    subject = f"Your Playground AI API Key - {safe_plan_tier} Plan"

    # Build email content with sanitized inputs
    html_content = f"""
    <html>
    <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <h2>Welcome to Playground AI!</h2>

        <p>Your <strong>{safe_plan_tier}</strong> subscription is now active.</p>

        <div style="background-color: #f5f5f5; padding: 20px; border-radius: 5px; margin: 20px 0;">
            <h3>Your API Key</h3>
            <code style="font-size: 14px; background: #fff; padding: 10px; display: block; border: 1px solid #ddd;">
                {safe_api_key}
            </code>
            <p style="color: #666; font-size: 12px; margin-top: 10px;">
                ⚠️ Keep this key secure! It will not be shown again.
            </p>
        </div>

        <h3>Plan Details</h3>
        <ul>
            <li><strong>Plan:</strong> {safe_plan_tier}</li>
            <li><strong>Price:</strong> ${monthly_price:.2f}/month</li>
            <li><strong>Monthly Tokens:</strong> 500,000</li>
            <li><strong>Payment Method:</strong> {safe_payment_method}</li>
        </ul>

        <h3>Getting Started</h3>
        <p>Visit our <a href="{settings.api_docs_url}">API Documentation</a> to start building.</p>

        <p>Base URL: <code>https://api.YOURDOMAIN.COM/v1</code></p>

        <h3>Example Usage</h3>
        <pre style="background: #f5f5f5; padding: 15px; border-radius: 5px; overflow-x: auto;">
from openai import OpenAI

client = OpenAI(
    api_key="{safe_api_key_preview}",
    base_url="https://api.YOURDOMAIN.COM/v1"
)

response = client.chat.completions.create(
    model="gpt-oss:20b",
    messages=[{{"role": "user", "content": "Hello!"}}]
)
        </pre>

        <p>Need help? Contact us at <a href="mailto:{settings.support_email}">{settings.support_email}</a></p>

        <p style="color: #666; font-size: 12px; margin-top: 40px;">
            Thank you for choosing Playground AI!
        </p>
    </body>
    </html>
    """

    plain_content = f"""
    Welcome to Playground AI!

    Your {plan_tier.replace('_', ' ').title()} subscription is now active.

    YOUR API KEY:
    {api_key}

    ⚠️ Keep this key secure! It will not be shown again.

    PLAN DETAILS:
    - Plan: {plan_tier.replace('_', ' ').title()}
    - Price: ${monthly_price:.2f}/month
    - Monthly Tokens: 500,000
    - Payment Method: {payment_method.replace('_', ' ').title()}

    GETTING STARTED:
    Visit our API Documentation: {settings.api_docs_url}
    Base URL: https://api.YOURDOMAIN.COM/v1

    Need help? Contact us at {settings.support_email}

    Thank you for choosing Playground AI!
    """

    # PGP encrypt if requested
    if pgp_encrypt and pgp_public_key:
        from services.pgp_handler import encrypt_message
        encrypted_content = await encrypt_message(plain_content, pgp_public_key)

        if encrypted_content:
            # Send encrypted version
            html_content = f"""
            <html>
            <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
                <h2>Your Playground AI API Key (PGP Encrypted)</h2>

                <p>Your API key has been encrypted with your PGP public key for maximum security.</p>

                <div style="background-color: #f5f5f5; padding: 20px; border-radius: 5px; margin: 20px 0;">
                    <h3>Encrypted Message</h3>
                    <pre style="font-size: 12px; background: #fff; padding: 10px; border: 1px solid #ddd; overflow-x: auto;">
{encrypted_content}
                    </pre>
                </div>

                <p>Decrypt this message with your private PGP key to access your API key.</p>

                <p>Need help? Contact us at <a href="mailto:{settings.support_email}">{settings.support_email}</a></p>
            </body>
            </html>
            """

            plain_content = f"PGP ENCRYPTED MESSAGE:\n\n{encrypted_content}\n\nDecrypt with your private key."

    return await send_email(to_email, subject, html_content, plain_content)


async def send_token_pack_receipt(
    to_email: str,
    pack_type: str,
    tokens_purchased: int,
    price_paid: float,
    currency: str,
    new_balance: int
) -> bool:
    """Send receipt for token pack purchase"""
    # Validate email
    valid, error = validate_email(to_email)
    if not valid:
        logger.error("invalid_email_for_receipt", email=to_email, error=error)
        return False

    to_email = sanitize_email(to_email)

    # Sanitize all inputs for HTML (prevent XSS)
    safe_pack_type = sanitize_html(pack_type.title())
    safe_currency = sanitize_html(currency.upper())

    subject = f"Token Pack Purchase Receipt - {safe_pack_type}"

    html_content = f"""
    <html>
    <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <h2>Token Pack Purchase Confirmed!</h2>

        <p>Thank you for your purchase.</p>

        <div style="background-color: #f5f5f5; padding: 20px; border-radius: 5px; margin: 20px 0;">
            <h3>Purchase Details</h3>
            <ul>
                <li><strong>Pack:</strong> {safe_pack_type}</li>
                <li><strong>Tokens:</strong> {tokens_purchased:,}</li>
                <li><strong>Price:</strong> ${price_paid:.2f} {safe_currency}</li>
            </ul>
        </div>

        <h3>Your New Balance</h3>
        <p><strong>{new_balance:,}</strong> tokens available</p>

        <p>These tokens never expire and can be used anytime.</p>

        <p>Questions? Contact us at <a href="mailto:{settings.support_email}">{settings.support_email}</a></p>
    </body>
    </html>
    """

    plain_content = f"""
    Token Pack Purchase Confirmed!

    PURCHASE DETAILS:
    - Pack: {pack_type.title()}
    - Tokens: {tokens_purchased:,}
    - Price: ${price_paid:.2f} {currency}

    YOUR NEW BALANCE:
    {new_balance:,} tokens available

    These tokens never expire and can be used anytime.

    Questions? Contact us at {settings.support_email}
    """

    return await send_email(to_email, subject, html_content, plain_content)


async def send_subscription_renewal_email(
    to_email: str,
    plan_tier: str,
    amount: float,
    next_billing_date: str
) -> bool:
    """Send subscription renewal confirmation"""
    # Validate email
    valid, error = validate_email(to_email)
    if not valid:
        logger.error("invalid_email_for_renewal", email=to_email, error=error)
        return False

    to_email = sanitize_email(to_email)

    # Sanitize all inputs for HTML (prevent XSS)
    safe_plan_tier = sanitize_html(plan_tier.replace('_', ' ').title())
    safe_next_billing_date = sanitize_html(next_billing_date)

    subject = "Subscription Renewed - Playground AI"

    html_content = f"""
    <html>
    <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <h2>Subscription Renewed</h2>

        <p>Your {safe_plan_tier} subscription has been renewed.</p>

        <ul>
            <li><strong>Amount:</strong> ${amount:.2f}</li>
            <li><strong>Monthly Tokens Reset:</strong> 500,000</li>
            <li><strong>Next Billing Date:</strong> {safe_next_billing_date}</li>
        </ul>

        <p>Thank you for continuing with Playground AI!</p>
    </body>
    </html>
    """

    plain_content = f"Your {plan_tier} subscription has been renewed for ${amount:.2f}. Monthly tokens reset to 500,000."

    return await send_email(to_email, subject, html_content, plain_content)
