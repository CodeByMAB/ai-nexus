"""
Security Headers Middleware
Adds comprehensive security headers to all HTTP responses
"""
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from typing import Callable
import structlog

logger = structlog.get_logger()


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Middleware to add security headers to all responses

    Security Headers Added:
    - Strict-Transport-Security: Force HTTPS
    - X-Content-Type-Options: Prevent MIME sniffing
    - X-Frame-Options: Prevent clickjacking
    - X-XSS-Protection: Enable XSS filter
    - Content-Security-Policy: Restrict resource loading
    - Referrer-Policy: Control referrer information
    - Permissions-Policy: Control browser features
    - X-Permitted-Cross-Domain-Policies: Block Adobe products
    """

    def __init__(self, app, hsts_max_age: int = 31536000, environment: str = "production"):
        """
        Initialize security headers middleware

        Args:
            app: FastAPI application
            hsts_max_age: HSTS max-age in seconds (default: 1 year)
            environment: Environment (production/development)
        """
        super().__init__(app)
        self.hsts_max_age = hsts_max_age
        self.is_production = environment.lower() == "production"

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """
        Process request and add security headers to response

        Args:
            request: Incoming request
            call_next: Next middleware/route handler

        Returns:
            Response with security headers
        """
        # Process the request
        response = await call_next(request)

        # Add security headers
        self._add_security_headers(response, request)

        return response

    def _add_security_headers(self, response: Response, request: Request):
        """
        Add all security headers to response

        Args:
            response: Response object
            request: Request object
        """
        headers = response.headers

        # 1. Strict-Transport-Security (HSTS)
        # Forces HTTPS for specified duration
        if self.is_production:
            headers["Strict-Transport-Security"] = (
                f"max-age={self.hsts_max_age}; "
                "includeSubDomains; "
                "preload"
            )

        # 2. X-Content-Type-Options
        # Prevents MIME type sniffing
        headers["X-Content-Type-Options"] = "nosniff"

        # 3. X-Frame-Options
        # Prevents clickjacking by disallowing iframe embedding
        headers["X-Frame-Options"] = "DENY"

        # 4. X-XSS-Protection
        # Enables browser's XSS filter (legacy, but still useful)
        headers["X-XSS-Protection"] = "1; mode=block"

        # 5. Content-Security-Policy
        # Restricts resource loading to prevent XSS
        # Only apply to HTML responses (not JSON API responses)
        content_type = headers.get("content-type", "")

        if "text/html" in content_type:
            # Strict CSP for HTML responses
            headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: https:; "
                "font-src 'self'; "
                "connect-src 'self'; "
                "frame-ancestors 'none'; "
                "base-uri 'self'; "
                "form-action 'self'"
            )
        else:
            # Lighter CSP for API responses
            headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"

        # 6. Referrer-Policy
        # Controls referrer information sent with requests
        headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # 7. Permissions-Policy (formerly Feature-Policy)
        # Restricts browser features
        headers["Permissions-Policy"] = (
            "geolocation=(), "
            "microphone=(), "
            "camera=(), "
            "payment=(), "
            "usb=(), "
            "magnetometer=(), "
            "gyroscope=(), "
            "accelerometer=()"
        )

        # 8. X-Permitted-Cross-Domain-Policies
        # Blocks Adobe Flash and PDF documents from loading data cross-domain
        headers["X-Permitted-Cross-Domain-Policies"] = "none"

        # 9. Cache-Control
        # Prevent caching of sensitive data
        if request.url.path.startswith("/api/"):
            headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
            headers["Pragma"] = "no-cache"
            headers["Expires"] = "0"

        # 10. Server header
        # Remove or obfuscate server information
        if "Server" in headers:
            del headers["Server"]

        # Optional: Add custom security header for identification
        headers["X-Content-Security-Policy"] = "default-src 'self'"
        headers["X-WebKit-CSP"] = "default-src 'self'"


def add_security_headers_middleware(app, environment: str = "production"):
    """
    Add security headers middleware to FastAPI app

    Args:
        app: FastAPI application instance
        environment: Environment (production/development)

    Example:
        from fastapi import FastAPI
        from middleware.security_headers import add_security_headers_middleware

        app = FastAPI()
        add_security_headers_middleware(app, environment="production")
    """
    app.add_middleware(
        SecurityHeadersMiddleware,
        hsts_max_age=31536000,  # 1 year
        environment=environment
    )

    logger.info(
        "security_headers_middleware_added",
        environment=environment,
        hsts_enabled=environment.lower() == "production"
    )
