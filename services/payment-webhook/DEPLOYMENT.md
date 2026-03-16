# Payment Webhook Service - Deployment Guide

## Overview

This service handles payment webhooks from Stripe, Strike, and BTCPay Server, automatically provisioning API keys and managing subscriptions.

## Prerequisites

✅ Python 3.12+
✅ nginx
✅ systemd
✅ Cloudflare Tunnel (for HTTPS)

## Installation Steps

### 1. Configure Environment Variables

Edit the `.env` file with your actual API keys:

```bash
cd /opt/ai/payment-webhook
nano .env
```

**Required settings:**
- `STRIPE_SECRET_KEY` - From https://dashboard.stripe.com/apikeys
- `STRIPE_WEBHOOK_SECRET` - From Stripe webhook settings
- `STRIKE_API_KEY` - From https://dashboard.strike.me/
- `SEND_GRID_API_KEY` - For email delivery

**Optional:**
- `BTCPAY_*` settings if using BTCPay Server

### 2. Create Log Directory

```bash
sudo mkdir -p /var/log/payment-webhook
sudo chown ai:ai /var/log/payment-webhook
```

### 3. Install systemd Service

```bash
sudo cp /opt/ai/payment-webhook/payment-webhook.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable payment-webhook
sudo systemctl start payment-webhook
```

### 4. Check Service Status

```bash
sudo systemctl status payment-webhook
```

You should see:
```
● payment-webhook.service - Playground AI Payment Webhook Service
   Loaded: loaded (/etc/systemd/system/payment-webhook.service)
   Active: active (running)
```

### 5. Update nginx Configuration

Add the webhook routes to your existing `api-subdomain.conf`:

```bash
sudo nano /etc/nginx/sites-available/api-subdomain.conf
```

Add the contents from `nginx-payment-webhook.conf` inside the existing `server` block (after the `/v1/` location).

Test nginx configuration:

```bash
sudo nginx -t
```

If successful, reload nginx:

```bash
sudo systemctl reload nginx
```

### 6. Configure Payment Providers

#### Stripe

1. Go to https://dashboard.stripe.com/webhooks
2. Click "Add endpoint"
3. URL: `https://api.YOURDOMAIN.COM/webhooks/stripe`
4. Events to send:
   - `checkout.session.completed`
   - `invoice.payment_succeeded`
   - `customer.subscription.deleted`
   - `invoice.payment_failed`
5. Copy the webhook signing secret to `.env` as `STRIPE_WEBHOOK_SECRET`

#### Strike

1. Go to https://dashboard.strike.me/
2. Navigate to API → Webhooks
3. Add webhook URL: `https://api.YOURDOMAIN.COM/webhooks/strike`
4. Enable `invoice.updated` event
5. Copy webhook secret to `.env` as `STRIKE_WEBHOOK_SECRET`

#### BTCPay Server (Optional)

1. Access your Start9 BTCPay instance
2. Go to Store Settings → Webhooks
3. Add webhook: `https://api.YOURDOMAIN.COM/webhooks/btcpay`
   - OR if TOR-only: Will need to set up polling instead
4. Enable invoice events

## Testing

### Test Health Check

```bash
curl https://api.YOURDOMAIN.COM/webhooks/health
```

Expected response:
```json
{
  "status": "healthy",
  "service": "payment-webhook",
  "version": "1.0.0"
}
```

### Test Promo Code API

```bash
curl https://api.YOURDOMAIN.COM/api/promo/FAMBAM2025
```

Should return promo code details.

### Test Stripe Webhook (Using Stripe CLI)

```bash
stripe listen --forward-to localhost:8003/webhooks/stripe
stripe trigger checkout.session.completed
```

### View Logs

```bash
# Service logs
sudo journalctl -u payment-webhook -f

# Application logs
tail -f /var/log/payment-webhook/service.log

# Errors
tail -f /var/log/payment-webhook/error.log
```

## Monitoring

### Check Service Status

```bash
sudo systemctl status payment-webhook
```

### Check if Port 8003 is Listening

```bash
ss -tlnp | grep 8003
```

Should show:
```
LISTEN  0  2048  0.0.0.0:8003  0.0.0.0:*  users:(("python3",pid=XXX,fd=X))
```

### Test Payment Webhook Endpoint

```bash
# Should return service info
curl http://localhost:8003/
```

## Troubleshooting

### Service Won't Start

```bash
# Check logs
sudo journalctl -u payment-webhook -n 50

# Check if port is already in use
ss -tlnp | grep 8003

# Test manually
cd /opt/ai/payment-webhook
source .venv/bin/activate
python3 app.py
```

### Webhooks Not Working

1. Check nginx is proxying correctly:
   ```bash
   curl -I https://api.YOURDOMAIN.COM/webhooks/health
   ```

2. Check webhook signature secrets are correct in `.env`

3. View real-time logs:
   ```bash
   sudo journalctl -u payment-webhook -f
   ```

### Database Errors

```bash
# Check database exists and is writable
ls -la /opt/ai/keys/keys.sqlite

# Check tables exist
sqlite3 /opt/ai/keys/keys.sqlite ".tables"
```

## Security Notes

- `.env` file permissions should be `600` (only owner can read/write)
- Webhook secrets should be strong and never committed to git
- Service runs as `ai` user (not root)
- nginx validates HTTPS certificates via Cloudflare
- Rate limiting is enabled on webhook endpoints

## Updating the Service

```bash
cd /opt/ai/payment-webhook

# Pull latest code (if using git)
# OR manually update files

# Restart service
sudo systemctl restart payment-webhook

# Check status
sudo systemctl status payment-webhook
```

## Environment Variables Reference

| Variable | Required | Description |
|----------|----------|-------------|
| `STRIPE_SECRET_KEY` | Yes* | Stripe API secret key |
| `STRIPE_WEBHOOK_SECRET` | Yes* | Stripe webhook signing secret |
| `STRIKE_API_KEY` | Yes* | Strike API key |
| `STRIKE_WEBHOOK_SECRET` | Yes* | Strike webhook secret |
| `SENDGRID_API_KEY` | Yes | SendGrid API key for emails |
| `EMAIL_FROM` | Yes | From email address |
| `BTCPAY_ENABLED` | No | Enable BTCPay integration |
| `DEBUG` | No | Enable debug mode (development only) |

*At least one payment provider (Stripe or Strike) required

## Architecture

```
Cloudflare Tunnel (HTTPS)
          ↓
nginx (port 8002) → /webhooks/* → Payment Webhook Service (port 8003)
                    /api/*
          ↓
Stripe/Strike/BTCPay Webhooks
          ↓
Subscription Manager
          ↓
API Key Provisioner → SQLite Database
          ↓
Email Delivery (SendGrid)
```

## Support

- Logs: `/var/log/payment-webhook/`
- Service status: `systemctl status payment-webhook`
- Database: `/opt/ai/keys/keys.sqlite`
- Config: `/opt/ai/payment-webhook/.env`
