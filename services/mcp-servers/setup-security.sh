#!/bin/bash

# MCP Server Security Setup Script
# This script helps you configure API key authentication

echo "🔒 MCP Server Security Setup"
echo "=============================="
echo ""

# Check if running from correct directory
if [ ! -f "docker-compose.yml" ]; then
    echo "❌ Error: Please run this script from /opt/ai/mcp-servers/"
    exit 1
fi

# Check if nginx.conf exists
if [ ! -f "nginx/nginx.conf" ]; then
    echo "❌ Error: nginx/nginx.conf not found"
    exit 1
fi

echo "This script will help you configure your API key for MCP server access."
echo ""
echo "📝 You'll need:"
echo "   - Your pg_ API key from the payment gateway"
echo ""

read -p "Do you have your API key ready? (y/n): " ready

if [ "$ready" != "y" ] && [ "$ready" != "Y" ]; then
    echo ""
    echo "Please get your pg_ API key first, then run this script again."
    echo "You can find it in: /opt/ai/payment-webhook/.env"
    echo "Look for lines starting with 'pg_'"
    exit 0
fi

echo ""
read -p "Enter your pg_ API key: " api_key

if [ -z "$api_key" ]; then
    echo "❌ Error: API key cannot be empty"
    exit 1
fi

if [[ ! $api_key == pg_* ]]; then
    echo "⚠️  Warning: API key doesn't start with 'pg_'"
    read -p "Continue anyway? (y/n): " continue
    if [ "$continue" != "y" ] && [ "$continue" != "Y" ]; then
        exit 0
    fi
fi

echo ""
echo "🔄 Updating nginx configuration..."

# Backup current config
cp nginx/nginx.conf nginx/nginx.conf.backup

# Replace the placeholder with actual API key
sed -i "s/CHANGE_ME_TO_YOUR_SECRET_KEY/$api_key/g" nginx/nginx.conf

if grep -q "$api_key" nginx/nginx.conf; then
    echo "✅ API key configured successfully"
else
    echo "❌ Error: Failed to update config"
    mv nginx/nginx.conf.backup nginx/nginx.conf
    exit 1
fi

echo ""
echo "🐳 Restarting nginx container..."
docker-compose up -d --build nginx-mcp-gateway

echo ""
echo "⏳ Waiting for nginx to start..."
sleep 5

echo ""
echo "🧪 Testing configuration..."

# Test without API key (should fail)
echo "   Testing without API key (should fail)..."
response=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8100/dockerhub/mcp)

if [ "$response" == "401" ]; then
    echo "   ✅ Unauthorized access blocked correctly"
else
    echo "   ⚠️  Warning: Expected 401, got $response"
fi

# Test with API key (should work)
echo "   Testing with API key (should work)..."
response=$(curl -s -H "X-API-Key: $api_key" -o /dev/null -w "%{http_code}" http://localhost:8100/health)

if [ "$response" == "200" ]; then
    echo "   ✅ API key authentication working"
else
    echo "   ❌ Error: Expected 200, got $response"
    echo "   Check logs: docker logs mcp-gateway"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ Security setup complete!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "📋 What was configured:"
echo "   ✓ API key authentication (X-API-Key header required)"
echo "   ✓ Rate limiting (10 req/s for /dockerhub, 2 req/s for /graphiti)"
echo "   ✓ Connection limiting (5 concurrent per IP)"
echo "   ✓ Security headers enabled"
echo ""
echo "🔑 Your API key: $api_key"
echo "   Store this securely! You'll need it for all requests."
echo ""
echo "📖 Next steps:"
echo "   1. Test locally:"
echo "      curl -H \"X-API-Key: $api_key\" http://localhost:8100/health"
echo ""
echo "   2. Configure Cloudflare Tunnel (recommended):"
echo "      - Add ingress: mcp.YOURDOMAIN.COM -> http://localhost:8100"
echo "      - See CLOUDFLARE_TUNNEL_SETUP.md for details"
echo ""
echo "   3. Add Cloudflare Access (strongly recommended):"
echo "      - See SECURITY_SETUP.md for full instructions"
echo ""
echo "   4. Save your API key to a password manager"
echo ""
echo "📚 Documentation:"
echo "   - Full guide: /opt/ai/mcp-servers/SECURITY_SETUP.md"
echo "   - Cloudflare setup: /opt/ai/mcp-servers/CLOUDFLARE_TUNNEL_SETUP.md"
echo ""
echo "🔍 Monitor access:"
echo "   docker logs mcp-gateway -f"
echo ""
