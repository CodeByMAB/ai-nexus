# Playground AI - Payment Webhook Service

## 🎉 Complete Payment System with Token Management

A comprehensive payment processing system supporting **Stripe**, **Strike** (USD & Bitcoin), and **BTCPay Server** (TOR), with automatic API key provisioning, token management, and PGP-encrypted delivery for privacy-focused users.

---

## ✨ Features

### Payment Processing
- ✅ **Stripe** - Credit card subscriptions and one-time payments
- ✅ **Strike** - Bitcoin/Lightning and USD payments (settles in BTC)
- ⏳ **BTCPay Server** - Self-hosted Bitcoin via TOR (framework ready)

### Subscription Plans
- **Trial**: $5/month (30-day limited access)
- **Family**: $18/month (with promo code FAMBAM2025)
- **Regular**: $25/month ⭐ Most popular
- **Ultra Privacy**: $30/month (TOR + PGP encryption)
- **Beta**: Free (with promo code VANGUARD)

**All plans include:** 500,000 tokens/month

### Token Packs (One-time purchases)
- **Trial**: 10k tokens @ $1.00
- **Small**: 100k tokens @ $5.00
- **Medium**: 500k tokens @ $20.00 (20% better value)
- **Large**: 1M tokens @ $35.00 (30% better value)

### Token Management
- Combined token pool (monthly + purchased)
- Monthly tokens reset automatically
- Purchased tokens never expire
- Real-time balance tracking
- Automatic monthly resets

### Security & Privacy
- Webhook signature verification (Stripe & Strike)
- PGP encryption for ultra-privacy tier
- Secure API key storage (hashed SHA256)
- HTTPS-only via Cloudflare Tunnel
- Rate limiting on all endpoints

---

## 📁 Project Structure

```
/opt/ai/payment-webhook/
├── app.py                          # Main FastAPI application
├── config.py                       # Configuration management
├── requirements.txt                # Python dependencies
├── .env                            # Environment variables (secure)
├── .env.example                    # Environment template
│
├── handlers/
│   ├── stripe_handler.py           # Stripe webhook processing
│   ├── strike_handler.py           # Strike webhook processing
│   └── btcpay_handler.py           # BTCPay integration (future)
│
├── services/
│   ├── key_provisioner.py          # API key generation
│   ├── token_manager.py            # Token accounting
│   ├── subscription_manager.py     # Subscription lifecycle
│   ├── promo_code_validator.py     # Promo code validation
│   ├── email_sender.py             # Email delivery (SendGrid)
│   └── pgp_handler.py              # PGP encryption
│
├── utils/
│   └── database.py                 # Database helpers
│
├── templates/
│   └── (email templates)           # HTML email templates
│
├── payment-webhook.service         # systemd service file
├── nginx-payment-webhook.conf      # nginx configuration
├── DEPLOYMENT.md                   # Deployment guide
└── README.md                       # This file
```

---

## 🗄️ Database Schema

### Tables Created
1. **api_keys** (extended with 12 new fields)
   - Payment fields: `payment_method`, `subscription_id`, `subscription_status`
   - Plan fields: `plan_tier`, `promo_code`, `monthly_price`
   - PGP fields: `pgp_public_key`, `pgp_fingerprint`
   - Token fields: `monthly_tokens_remaining`, `purchased_tokens_remaining`, `tokens_used_this_month`

2. **payment_events** (new)
   - Logs all payment transactions
   - Tracks subscriptions and token pack purchases
   - Stores webhook payloads for debugging

3. **token_purchases** (new)
   - History of token pack purchases
   - Links to payment events
   - Tracks pack type and tokens added

4. **promo_codes** (new)
   - Active codes: `FAMBAM2025`, `VANGUARD`
   - Tracks usage limits and expiration
   - Supports both fixed and percentage discounts

---

## 🚀 Quick Start

### 1. Configure Environment

```bash
cd /opt/ai/payment-webhook
cp .env.example .env
nano .env  # Add your API keys
```

Required:
- `STRIPE_SECRET_KEY` & `STRIPE_WEBHOOK_SECRET`
- `STRIKE_API_KEY` & `STRIKE_WEBHOOK_SECRET`
- `SENDGRID_API_KEY`

### 2. Install Service

```bash
# Create log directory
sudo mkdir -p /var/log/payment-webhook
sudo chown ai:ai /var/log/payment-webhook

# Install systemd service
sudo cp payment-webhook.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable payment-webhook
sudo systemctl start payment-webhook

# Check status
sudo systemctl status payment-webhook
```

### 3. Configure nginx

Add the webhook routes to `/etc/nginx/sites-available/api-subdomain.conf`:

```bash
# Copy the routes from nginx-payment-webhook.conf
# into the existing server block

sudo nginx -t
sudo systemctl reload nginx
```

### 4. Configure Payment Providers

**Stripe:**
- Dashboard: https://dashboard.stripe.com/webhooks
- Webhook URL: `https://api.YOURDOMAIN.COM/webhooks/stripe`
- Events: `checkout.session.completed`, `invoice.payment_succeeded`, `customer.subscription.deleted`, `invoice.payment_failed`

**Strike:**
- Dashboard: https://dashboard.strike.me/
- Webhook URL: `https://api.YOURDOMAIN.COM/webhooks/strike`
- Events: `invoice.updated`

---

## 📡 API Endpoints

### Webhooks (for payment providers)
- `POST /webhooks/stripe` - Stripe webhook receiver
- `POST /webhooks/strike` - Strike webhook receiver
- `POST /webhooks/btcpay` - BTCPay webhook receiver

### Public API (for sign-up website)
- `POST /api/create-payment` - Create payment intent/invoice
- `GET /api/promo/{code}` - Validate promo code
- `GET /api/subscription/status/{email}` - Get subscription status

### Health & Info
- `GET /health` - Health check
- `GET /` - Service information

---

## 🧪 Testing

### Test Services
```bash
cd /opt/ai/payment-webhook
source .venv/bin/activate

# Test foundation
python3 test_foundation.py

# Test core services
python3 test_services.py
```

### Test API Endpoints
```bash
# Health check
curl https://api.YOURDOMAIN.COM/webhooks/health

# Check promo code
curl https://api.YOURDOMAIN.COM/api/promo/FAMBAM2025

# Test webhook (local)
curl -X POST http://localhost:8003/webhooks/stripe \
  -H "Content-Type: application/json" \
  -d '{"type":"test"}'
```

### Test with Stripe CLI
```bash
stripe listen --forward-to localhost:8003/webhooks/stripe
stripe trigger checkout.session.completed
```

---

## 📊 Monitoring

### Service Status
```bash
sudo systemctl status payment-webhook
```

### Logs
```bash
# Real-time logs
sudo journalctl -u payment-webhook -f

# Application logs
tail -f /var/log/payment-webhook/service.log

# Error logs
tail -f /var/log/payment-webhook/error.log
```

### Database Stats
```bash
sqlite3 /opt/ai/keys/keys.sqlite <<EOF
SELECT 'API Keys:', COUNT(*) FROM api_keys;
SELECT 'Payment Events:', COUNT(*) FROM payment_events;
SELECT 'Token Purchases:', COUNT(*) FROM token_purchases;
SELECT 'Promo Codes:', COUNT(*) FROM promo_codes WHERE is_active=1;
EOF
```

---

## 🔐 Security

- ✅ Webhook signatures verified (Stripe HMAC, Strike HMAC)
- ✅ API keys hashed with SHA256
- ✅ HTTPS enforced via Cloudflare
- ✅ PGP encryption for ultra-privacy users
- ✅ Service runs as non-root user (`ai`)
- ✅ Environment variables stored securely (`.env` with 600 permissions)
- ✅ Rate limiting enabled
- ✅ Structured logging for audit trails

---

## 🎯 Integration with AI Gateway

The AI Gateway (`/opt/ai/gateway`) has been updated to use the new token system:

- ✅ Checks combined token pool (monthly + purchased)
- ✅ Validates subscription status
- ✅ Enforces token balance before API calls
- ✅ Backward compatible with legacy limits

Backup of original: `/opt/ai/gateway/auth_mw.py.backup`

---

## 📈 Token Economics

### Subscription Value
All plans get 500k tokens/month:
- **Trial**: $5/month = $0.01 per 1k tokens
- **Family**: $18/month = $0.036 per 1k tokens (with promo)
- **Regular**: $25/month = $0.05 per 1k tokens
- **Ultra Privacy**: $30/month = $0.06 per 1k tokens + privacy features

### Token Pack Value
- **Trial**: $0.10 per 1k tokens
- **Small**: $0.05 per 1k tokens (same as subscription)
- **Medium**: $0.04 per 1k tokens (20% better value)
- **Large**: $0.035 per 1k tokens (30% better value)

**Best value:** Large pack or Regular subscription

---

## 🆘 Troubleshooting

See [DEPLOYMENT.md](DEPLOYMENT.md) for detailed troubleshooting guide.

Common issues:
- **Service won't start**: Check `.env` configuration
- **Webhooks not working**: Verify webhook secrets
- **Token errors**: Check database migration completed
- **Email not sending**: Verify SendGrid API key

---

## 📝 What Was Built

### Core Services (9 modules)
1. ✅ Token Manager - Token accounting & monthly resets
2. ✅ Key Provisioner - API key generation & management
3. ✅ Subscription Manager - Subscription lifecycle
4. ✅ Promo Code Validator - Discount code validation
5. ✅ Email Sender - SendGrid integration
6. ✅ PGP Handler - Encryption for privacy users
7. ✅ Stripe Handler - Credit card processing
8. ✅ Strike Handler - Bitcoin/Lightning payments
9. ✅ Database Utilities - Async database helpers

### Configuration & Deployment
- ✅ Environment management (Pydantic Settings)
- ✅ systemd service file
- ✅ nginx configuration
- ✅ Structured logging (structlog)
- ✅ Comprehensive error handling

### Database
- ✅ Migration script for payment system
- ✅ 3 new tables (payment_events, token_purchases, promo_codes)
- ✅ 12 new columns in api_keys table
- ✅ Proper indexes for performance

### Testing
- ✅ Foundation test suite
- ✅ Services test suite
- ✅ All tests passing

---

## 🎉 Ready for Production!

The payment webhook service is **fully functional** and ready to:
1. ✅ Accept Stripe payments
2. ✅ Accept Strike payments (USD & Bitcoin)
3. ✅ Provision API keys automatically
4. ✅ Manage subscriptions & renewals
5. ✅ Handle token pack purchases
6. ✅ Send email confirmations
7. ✅ Support PGP encryption
8. ✅ Validate promo codes
9. ✅ Track all payment events

### Next Steps:
1. Configure your Stripe & Strike accounts
2. Add webhook URLs in payment provider dashboards
3. Test with real payments in test mode
4. Deploy to production!

---

## 📞 Support

- Configuration: `/opt/ai/payment-webhook/.env`
- Logs: `/var/log/payment-webhook/`
- Database: `/opt/ai/keys/keys.sqlite`
- Documentation: `DEPLOYMENT.md`

Built with ❤️ using FastAPI, Stripe, Strike, and SQLite.
