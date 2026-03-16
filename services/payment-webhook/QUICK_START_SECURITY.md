# Quick Start - Security Setup

**5-Minute Security Configuration Guide**

---

## Step 1: Generate Admin API Key (30 seconds)

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Copy the output (e.g., `sCqVcc5ycxzg8jpI5uHMXhdZxxvCnktC6FW4JD0Bdk_lrCOU7MDTKfM1giL8M6Pt`)

---

## Step 2: Update Environment File (1 minute)

```bash
cd /opt/ai/payment-webhook
nano .env
```

Add this line:
```bash
ADMIN_API_KEYS=paste_your_generated_key_here
```

Save and exit (`Ctrl+X`, `Y`, `Enter`)

---

## Step 3: Restart Service (30 seconds)

```bash
sudo systemctl restart payment-webhook
sudo systemctl status payment-webhook
```

Look for: `● payment-webhook.service - Payment Webhook Service`
Status should be: `Active: active (running)`

---

## Step 4: Test Security (2 minutes)

### Test Rate Limiting
```bash
# This should work (under limit)
curl https://api.YOURDOMAIN.COM/health

# Run this 15 times - last few should return 429 (rate limited)
for i in {1..15}; do curl https://api.YOURDOMAIN.COM/health; echo; done
```

### Test Authentication
```bash
# This should return 401 (Unauthorized)
curl https://api.YOURDOMAIN.COM/api/subscription/status/test@example.com

# This should work (with your key)
curl -H "X-API-Key: YOUR_KEY_HERE" \
  https://api.YOURDOMAIN.COM/api/subscription/status/test@example.com
```

### Test Security Headers
```bash
# Check headers are present
curl -I https://api.YOURDOMAIN.COM/health | grep -E "X-Content-Type|X-Frame|Strict-Transport"
```

---

## What's Now Protected

### ✅ Rate Limiting Active
- Webhooks: 10/min
- Create Payment: 20/min
- Promo Check: 60/min
- Status Check: 30/min
- Health: 100/min

### ✅ Authentication Required
- `/api/subscription/status/{email}` now requires API key

### ✅ Input Validation
- All inputs sanitized
- SQL injection: BLOCKED
- XSS attacks: BLOCKED
- Webhook replays: BLOCKED

### ✅ PGP Security
- Key format validated
- Command injection: BLOCKED
- Size limits enforced

### ✅ Secure Logging
- API keys auto-redacted
- Emails partially hidden
- Secrets never logged

### ✅ Security Headers
- HSTS: Enabled
- XSS Protection: Enabled
- Clickjacking: BLOCKED
- MIME Sniffing: BLOCKED

---

## Quick Troubleshooting

### Problem: 401 Unauthorized on status endpoint
**Solution:** Add API key to request:
```bash
curl -H "X-API-Key: YOUR_KEY" https://...
```

### Problem: 429 Too Many Requests
**Solution:** You're being rate limited. Wait 1 minute and try again.

### Problem: Service won't start
**Check logs:**
```bash
journalctl -u payment-webhook -n 50
```

### Problem: Invalid admin API key
**Re-generate and update:**
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
# Update .env with new key
sudo systemctl restart payment-webhook
```

---

## Daily Operations

### View Logs
```bash
# Real-time logs
journalctl -u payment-webhook -f

# Last 100 lines
journalctl -u payment-webhook -n 100

# Filter for errors
journalctl -u payment-webhook | grep -i error
```

### Check Service Status
```bash
sudo systemctl status payment-webhook
```

### Restart Service
```bash
sudo systemctl restart payment-webhook
```

---

## Security Monitoring Commands

### Check Rate Limit Blocks
```bash
journalctl -u payment-webhook | grep "rate_limit_exceeded"
```

### Check Authentication Failures
```bash
journalctl -u payment-webhook | grep "invalid_api_key_attempt"
```

### Check Input Validation Blocks
```bash
journalctl -u payment-webhook | grep -E "invalid_email|invalid_plan_tier"
```

### Check PGP Validation
```bash
journalctl -u payment-webhook | grep "pgp_key_validation_failed"
```

---

## Emergency Procedures

### If Under Attack (DDoS)
```bash
# Check access logs
journalctl -u payment-webhook -n 1000 | grep -oP 'IP: \K[0-9.]+' | sort | uniq -c | sort -nr

# Block IP in nginx if needed (temporary)
sudo nano /etc/nginx/sites-enabled/api-subdomain.conf
# Add: deny 1.2.3.4;
sudo nginx -t && sudo systemctl reload nginx
```

### If API Key Compromised
```bash
# 1. Generate new key
python3 -c "import secrets; print(secrets.token_urlsafe(48))"

# 2. Add new key alongside old (allows rotation)
nano /opt/ai/payment-webhook/.env
# ADMIN_API_KEYS=old_key,new_key

# 3. Restart
sudo systemctl restart payment-webhook

# 4. Update all clients to use new key

# 5. Remove old key from .env
# 6. Restart again
```

---

## Security Checklist (Weekly)

- [ ] Review authentication failure logs
- [ ] Check rate limit hits
- [ ] Verify no sensitive data in logs
- [ ] Check service is running
- [ ] Review webhook event deduplication
- [ ] Scan with https://securityheaders.com

---

## Need Help?

### Documentation
- Security Audit: `SECURITY_AUDIT.md`
- Full Completion Report: `SECURITY_COMPLETION_SUMMARY.md`
- Auth Setup Guide: `AUTHENTICATION_SETUP.md`

### Check Configuration
```bash
# View current config (redacted)
cat /opt/ai/payment-webhook/.env | grep -v "SECRET\|KEY\|PASSWORD"
```

### Test All Security Features
```bash
cd /opt/ai/payment-webhook
python3 << 'EOF'
import requests
import time

base = "https://api.YOURDOMAIN.COM"

# Test 1: Rate limiting
print("Testing rate limiting...")
for i in range(15):
    r = requests.get(f"{base}/health")
    print(f"  Request {i+1}: {r.status_code}")
    if r.status_code == 429:
        print("  ✅ Rate limiting working!")
        break

time.sleep(2)

# Test 2: Authentication
print("\nTesting authentication...")
r = requests.get(f"{base}/api/subscription/status/test@example.com")
if r.status_code == 401:
    print("  ✅ Authentication required!")
else:
    print(f"  ❌ Expected 401, got {r.status_code}")

# Test 3: Security headers
print("\nTesting security headers...")
r = requests.get(f"{base}/health")
headers_to_check = [
    'X-Content-Type-Options',
    'X-Frame-Options',
    'X-XSS-Protection'
]
for header in headers_to_check:
    if header in r.headers:
        print(f"  ✅ {header}: {r.headers[header]}")
    else:
        print(f"  ❌ {header}: Missing")

print("\n✅ Security checks complete!")
EOF
```

---

**Last Updated:** 2025-10-22
**Status:** Production Ready
**Security Level:** Maximum
