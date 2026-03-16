"""
Configuration Management
Loads and validates environment variables using Pydantic Settings
"""
from typing import Optional, List
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, validator


class Settings(BaseSettings):
    """Application configuration from environment variables"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False
    )

    # Database
    key_db_path: str = Field(default="/opt/ai/keys/keys.sqlite", description="Path to SQLite database")

    # Server
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8003)
    log_level: str = Field(default="info")
    environment: str = Field(default="production")  # development, staging, production

    # Stripe
    stripe_secret_key: Optional[str] = Field(default=None)
    stripe_publishable_key: Optional[str] = Field(default=None)
    stripe_webhook_secret: Optional[str] = Field(default=None)
    stripe_api_version: str = Field(default="2023-10-16")

    # Strike
    strike_api_key: Optional[str] = Field(default=None)
    strike_webhook_secret: Optional[str] = Field(default=None)
    strike_api_base_url: str = Field(default="https://api.strike.me")

    # BTCPay (Optional)
    btcpay_enabled: bool = Field(default=False)
    btcpay_onion_url: Optional[str] = Field(default=None)
    btcpay_api_key: Optional[str] = Field(default=None)
    btcpay_store_id: Optional[str] = Field(default=None)
    btcpay_webhook_secret: Optional[str] = Field(default=None)
    btcpay_use_polling: bool = Field(default=True)
    btcpay_poll_interval: int = Field(default=15)  # seconds

    # Pricing - Subscription Plans
    plan_trial_price: float = Field(default=5.00)
    plan_family_price: float = Field(default=18.00)
    plan_regular_price: float = Field(default=25.00)
    plan_ultra_privacy_price: float = Field(default=30.00)
    plan_beta_price: float = Field(default=0.00)
    monthly_token_allowance: int = Field(default=500000)

    # Pricing - Token Packs
    pack_trial_tokens: int = Field(default=10000)
    pack_trial_price: float = Field(default=1.00)
    pack_small_tokens: int = Field(default=100000)
    pack_small_price: float = Field(default=5.00)
    pack_medium_tokens: int = Field(default=500000)
    pack_medium_price: float = Field(default=20.00)
    pack_large_tokens: int = Field(default=1000000)
    pack_large_price: float = Field(default=35.00)

    # Email
    email_provider: str = Field(default="sendgrid")  # sendgrid, smtp, mailgun, postmark
    email_from: str = Field(default="noreply@YOURDOMAIN.COM")
    email_from_name: str = Field(default="Playground AI")

    # SendGrid
    sendgrid_api_key: Optional[str] = Field(default=None)

    # SMTP
    smtp_host: Optional[str] = Field(default=None)
    smtp_port: int = Field(default=587)
    smtp_user: Optional[str] = Field(default=None)
    smtp_password: Optional[str] = Field(default=None)
    smtp_tls: bool = Field(default=True)

    # PGP
    gpg_home: str = Field(default="/opt/ai/payment-webhook/.gnupg")
    gpg_key_bits: int = Field(default=4096)
    gpg_enable_encryption: bool = Field(default=True)

    # Website URLs
    signup_website_url: str = Field(default="https://YOURDOMAIN.COM")
    api_docs_url: str = Field(default="https://api.YOURDOMAIN.COM/docs")
    support_email: str = Field(default="support@YOURDOMAIN.COM")

    # Security
    cors_origins: str = Field(default="https://YOURDOMAIN.COM,https://ai.YOURDOMAIN.COM")
    admin_api_keys: Optional[str] = Field(default=None, description="Comma-separated list of admin API keys for protected endpoints")
    rate_limit_enabled: bool = Field(default=True)
    rate_limit_requests: int = Field(default=100)
    rate_limit_period: int = Field(default=60)  # seconds

    # Monitoring
    enable_structured_logging: bool = Field(default=True)
    sentry_dsn: Optional[str] = Field(default=None)
    log_file_path: str = Field(default="/opt/ai/payment-webhook/logs/webhook.log")

    # Development
    debug: bool = Field(default=False)
    test_mode: bool = Field(default=False)
    mock_emails: bool = Field(default=False)

    @property
    def cors_origins_list(self) -> List[str]:
        """Parse CORS origins from comma-separated string"""
        return [origin.strip() for origin in self.cors_origins.split(",")]

    @property
    def is_production(self) -> bool:
        """Check if running in production"""
        return self.environment.lower() == "production"

    @property
    def is_development(self) -> bool:
        """Check if running in development"""
        return self.environment.lower() == "development"

    def get_plan_price(self, plan_tier: str) -> float:
        """Get price for a subscription plan"""
        price_map = {
            "trial": self.plan_trial_price,
            "family": self.plan_family_price,
            "regular": self.plan_regular_price,
            "ultra_privacy": self.plan_ultra_privacy_price,
            "beta": self.plan_beta_price,
        }
        return price_map.get(plan_tier, self.plan_regular_price)

    def get_pack_details(self, pack_type: str) -> dict:
        """Get token pack details"""
        pack_map = {
            "trial": {"tokens": self.pack_trial_tokens, "price": self.pack_trial_price},
            "small": {"tokens": self.pack_small_tokens, "price": self.pack_small_price},
            "medium": {"tokens": self.pack_medium_tokens, "price": self.pack_medium_price},
            "large": {"tokens": self.pack_large_tokens, "price": self.pack_large_price},
        }
        pack_details = pack_map.get(pack_type)
        if pack_details:
            pack_details["type"] = pack_type
            pack_details["cost_per_1k"] = pack_details["price"] / (pack_details["tokens"] / 1000)
        return pack_details


# Global settings instance
settings = Settings()


# Validation on import
def validate_config():
    """Validate critical configuration on startup"""
    errors = []

    # Check payment providers
    if not settings.stripe_secret_key and not settings.strike_api_key:
        errors.append("At least one payment provider (Stripe or Strike) must be configured")

    # Check email provider
    if settings.email_provider == "sendgrid" and not settings.sendgrid_api_key:
        errors.append("SendGrid API key required when EMAIL_PROVIDER=sendgrid")
    elif settings.email_provider == "smtp" and not settings.smtp_host:
        errors.append("SMTP host required when EMAIL_PROVIDER=smtp")

    # Check BTCPay if enabled
    if settings.btcpay_enabled:
        if not settings.btcpay_api_key or not settings.btcpay_store_id:
            errors.append("BTCPay API key and Store ID required when BTCPAY_ENABLED=true")

    # Check database path
    import os
    if not os.path.exists(settings.key_db_path):
        errors.append(f"Database not found at {settings.key_db_path}")

    if errors and settings.is_production:
        raise ValueError(f"Configuration errors:\n" + "\n".join(f"  • {err}" for err in errors))
    elif errors:
        print("⚠️  Configuration warnings:")
        for err in errors:
            print(f"  • {err}")


# Run validation (will raise error in production if misconfigured)
if __name__ != "pytest":  # Skip validation during tests
    validate_config()
