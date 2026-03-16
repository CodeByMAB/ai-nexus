# MCP Server Security Setup Guide

## 🔒 Security Measures Implemented

### 1. API Key Authentication ✅
All MCP endpoints now require an API key via the `X-API-Key` header.

### 2. Rate Limiting ✅
- Docker Hub MCP: 10 requests/second, burst up to 20
- Graphiti MCP: 2 requests/second, burst up to 5 (stricter due to sensitive data)
- Health endpoint: 2 requests/second, burst up to 5

### 3. Connection Limiting ✅
- Maximum 5 concurrent connections per IP address

### 4. Security Headers ✅
- X-Frame-Options: DENY
- X-Content-Type-Options: nosniff
- X-XSS-Protection: enabled
- Referrer-Policy: no-referrer

## 🔑 Setup Your API Key

### Step 1: Update Nginx Configuration

Edit the nginx config to use YOUR API key:

```bash
cd /opt/ai/mcp-servers
nano nginx/nginx.conf
```

Find this line (around line 20):
```nginx
"CHANGE_ME_TO_YOUR_SECRET_KEY" 1;
```

Replace `CHANGE_ME_TO_YOUR_SECRET_KEY` with your `pg_` API key:
```nginx
"pg_YourActualAPIKeyHere" 1;
```

Save and exit (Ctrl+X, Y, Enter).

### Step 2: Rebuild and Restart

```bash
cd /opt/ai/mcp-servers
docker-compose up -d --build nginx-mcp-gateway
```

### Step 3: Verify Security

Test without API key (should fail):
```bash
curl -s http://localhost:8100/dockerhub/mcp
```

Expected response:
```json
{"error":"Unauthorized - Invalid or missing X-API-Key header"}
```

Test with API key (should work):
```bash
curl -s http://localhost:8100/dockerhub/mcp \
  -H "X-API-Key: pg_YourActualAPIKeyHere"
```

Expected response:
```json
{"error":{"message":"Method not allowed."}}
```
(This is normal - it needs POST with proper MCP payload)

Full test with proper request:
```bash
curl -X POST http://localhost:8100/dockerhub/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-API-Key: pg_YourActualAPIKeyHere" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
```

Should return successful initialization response.

## 🌐 Cloudflare Access (Additional Layer)

For even more security, add Cloudflare Access:

### Step 1: Create Cloudflare Access Application

1. Go to https://one.dash.cloudflare.com/
2. Navigate to **Zero Trust** → **Access** → **Applications**
3. Click **Add an application**
4. Select **Self-hosted**

### Step 2: Configure Application

**Application Configuration:**
- **Application name:** MCP Servers
- **Session duration:** 24 hours
- **Application domain:**
  - Subdomain: `mcp`
  - Domain: `YOURDOMAIN.COM`

**Application appearance:**
- **App Launcher visibility:** Visible (or hidden if you prefer)
- **Logo:** (optional)

Click **Next**

### Step 3: Add Policy

**Policy name:** Allow My Email Only

**Configure rules:**
- **Selector:** Emails
- **Value:** your-email@example.com
- **Action:** Allow

**Or use One-Time PIN:**
- **Selector:** Emails
- **Value:** your-email@example.com
- **Action:** Allow
- **Additional settings:** Require one-time PIN

Click **Next**, then **Add application**

### Step 4: Test Access

1. Visit https://mcp.YOURDOMAIN.COM/health
2. You should be redirected to Cloudflare login
3. Enter your email
4. Check your email for PIN (if using OTP)
5. After login, you'll reach the health endpoint

## 🔐 Multi-Layer Security Active

With both API key + Cloudflare Access:

**Layer 1: Cloudflare Access**
- User must authenticate with email
- Session lasts 24 hours
- Can be revoked anytime

**Layer 2: API Key**
- Every request needs `X-API-Key` header
- Independent of Cloudflare session
- Can be rotated

**Layer 3: Rate Limiting**
- Prevents abuse even with valid credentials
- Different limits per endpoint

**Layer 4: Connection Limiting**
- Max 5 concurrent connections per IP
- Prevents resource exhaustion

## 📊 Monitoring

### Check Access Logs

```bash
# Real-time monitoring
docker exec mcp-gateway tail -f /var/log/nginx/access.log

# Check for failed auth attempts
docker exec mcp-gateway grep "401" /var/log/nginx/access.log

# Check rate limit violations
docker exec mcp-gateway grep "503" /var/log/nginx/access.log
```

### Cloudflare Logs

1. Go to Cloudflare Dashboard
2. Navigate to **Analytics** → **Logs**
3. Filter by `mcp.YOURDOMAIN.COM`
4. Look for:
   - 401 errors (failed auth)
   - 503 errors (rate limit exceeded)
   - Unusual IP addresses
   - High request volumes

### Set Up Alerts (Optional)

**Cloudflare Notifications:**
1. Dashboard → **Notifications**
2. **Add** → **HTTP DDoS Attack**
3. Configure for `mcp.YOURDOMAIN.COM`

## 🚨 Security Checklist

Before exposing via Cloudflare Tunnel:

- [ ] API key configured in nginx.conf
- [ ] nginx container restarted
- [ ] Tested API key authentication works
- [ ] Tested unauthorized requests are blocked
- [ ] Cloudflare Access configured (recommended)
- [ ] Monitoring set up
- [ ] API key stored securely (password manager)
- [ ] No sensitive data in Graphiti knowledge graph
- [ ] DOCKERHUB_PAT not configured (unless needed)

## 🔄 API Key Rotation

To rotate your API key:

1. Generate new API key (or use another `pg_` key)
2. Update `nginx/nginx.conf` with new key
3. Rebuild nginx: `docker-compose up -d --build nginx-mcp-gateway`
4. Update clients with new key
5. Old key is immediately invalidated

## 🛡️ Best Practices

### DO:
✅ Use Cloudflare Access for additional auth layer
✅ Keep API key in password manager
✅ Monitor access logs regularly
✅ Use HTTPS only (via Cloudflare)
✅ Rotate API key periodically (quarterly)
✅ Keep nginx/Docker images updated

### DON'T:
❌ Share your API key publicly
❌ Commit API key to git repositories
❌ Use same API key across multiple services
❌ Expose port 8100 directly (only via Cloudflare)
❌ Disable rate limiting
❌ Store sensitive data in Graphiti without encryption

## 🔧 Troubleshooting

### "Unauthorized" Error
**Problem:** Getting 401 even with API key

**Solutions:**
1. Check API key is correct (copy-paste from source)
2. Ensure using header `X-API-Key` (exact capitalization)
3. Check nginx config has your API key
4. Restart nginx: `docker-compose restart nginx-mcp-gateway`
5. Check logs: `docker logs mcp-gateway`

### "Service Unavailable" (503)
**Problem:** Rate limit exceeded

**Solutions:**
1. Wait a few seconds
2. Reduce request frequency
3. Check if you have multiple clients accessing
4. Review rate limit settings in nginx.conf

### Cloudflare Access Loop
**Problem:** Keeps redirecting to login

**Solutions:**
1. Clear browser cookies for `YOURDOMAIN.COM`
2. Try incognito/private browsing
3. Check Cloudflare Access policy is correct
4. Verify email is in allowed list

## 📝 Configuration Files

**Nginx config:** `/opt/ai/mcp-servers/nginx/nginx.conf`
**Docker compose:** `/opt/ai/mcp-servers/docker-compose.yml`
**Logs:** `docker logs mcp-gateway`

## 🎯 Current Security Status

**Risk Level:** LOW ✅ (with API key + Cloudflare Access)

**Protected:**
- ✅ Unauthorized access blocked
- ✅ Rate limiting active
- ✅ Connection limiting active
- ✅ Security headers enabled
- ✅ Logging enabled

**Recommended Next Step:**
Add Cloudflare Access for defense-in-depth

## 📞 Quick Reference

**Test API key:**
```bash
curl -H "X-API-Key: YOUR_KEY" http://localhost:8100/health
```

**View logs:**
```bash
docker logs mcp-gateway -f
```

**Restart nginx:**
```bash
docker-compose restart nginx-mcp-gateway
```

**Check config:**
```bash
docker exec mcp-gateway nginx -t
```

---

**Status:** 🔒 Secured and ready for personal use
