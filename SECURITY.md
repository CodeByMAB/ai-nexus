# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.x     | ✅ Yes    |

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Email security reports to: **security@8ase0f0ps.COM** // TBD

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

You will receive a response within 48 hours. We aim to release patches for critical issues within 7 days.

## Security Architecture

### Authentication
- Dashboard: bcrypt passwords + mandatory TOTP 2FA + optional WebAuthn/FIDO2 (YubiKey, passkeys)
- API Gateway: `pg_`-prefixed bearer tokens validated via SHA256 hash against SQLite
- All sessions: JWT in `HttpOnly; Secure; SameSite=Strict` cookies

### Credential Management
- **Never** commit `.env` files — use `.env.example` as template
- Generate all secrets with `openssl rand -base64 32` (minimum 32 bytes)
- Rotate API keys regularly via the dashboard Keys tab
- The `keys.sqlite` database should have permissions `640` (`chown ai:ai`)

### Network Security
- All public traffic via Cloudflare Tunnel (no exposed ports required)
- Internal services bound to `127.0.0.1` only
- nginx reverse proxy with security headers (HSTS, CSP, X-Frame-Options)
- Rate limiting on all authentication endpoints (10 attempts/15 min per IP)

### Known Security Considerations

1. **Model weights are not scanned** — only download models from trusted sources (HuggingFace verified publishers)
2. **Neo4j Community Edition** has no row-level security — treat it as internal-only
3. **Stripe/Strike webhooks** — validate webhook signatures in `payment-webhook/utils/security.py`
4. **LLM prompt injection** — the gateway does not sanitize user prompts; implement application-level filtering for multi-tenant deployments
5. **Docker socket** — the `ai` user requires Docker group membership; this grants effective root for container operations

### Dependency Scanning

This project includes:
- TruffleHog secret scanning on every push (`.github/workflows/secret-scan.yml`)
- Gitleaks pre-commit compatible scanning
- Manual SBOM review in `SBOM.json`

Run locally:
```bash
# Install gitleaks
brew install gitleaks   # macOS
# or: https://github.com/gitleaks/gitleaks/releases

# Scan repo
gitleaks detect --source . --verbose
```
