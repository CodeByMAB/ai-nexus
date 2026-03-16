#!/bin/bash
# AI Dashboard Setup Script
# Run as ai: bash /opt/ai/dashboard/setup.sh
# This script does NOT require sudo.

set -euo pipefail

DASHBOARD_DIR="/opt/ai/dashboard"
VENV="$DASHBOARD_DIR/.venv"

echo "=== AI Dashboard Setup ==="

# --- Python venv ---
if [ ! -d "$VENV" ]; then
    echo "[1/5] Creating Python virtual environment..."
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --upgrade pip --quiet
    echo "[2/5] Installing Python dependencies..."
    "$VENV/bin/pip" install -r "$DASHBOARD_DIR/requirements.txt" --quiet
else
    echo "[1/5] Venv already exists."
    echo "[2/5] Updating Python dependencies..."
    "$VENV/bin/pip" install -r "$DASHBOARD_DIR/requirements.txt" --quiet
fi

echo "      Done."

# --- Data directory ---
echo "[3/5] Setting up data directory..."
mkdir -p "$DASHBOARD_DIR/data"
chmod 750 "$DASHBOARD_DIR/data"
echo "      Done."

# --- Systemd user service ---
echo "[4/5] Installing systemd user service..."
mkdir -p "$HOME/.config/systemd/user"

cat > "$HOME/.config/systemd/user/ai-dashboard.service" << 'EOF'
[Unit]
Description=AI System Dashboard
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/ai/dashboard
ExecStart=/opt/ai/dashboard/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8200 --workers 1
Restart=on-failure
RestartSec=5
Environment=DASHBOARD_ENV=production
Environment=HOME=${HOME}

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable ai-dashboard
systemctl --user restart ai-dashboard
sleep 2

STATUS=$(systemctl --user is-active ai-dashboard 2>/dev/null || echo "unknown")
if [ "$STATUS" = "active" ]; then
    echo "      Service started successfully."
else
    echo "      WARNING: Service status: $STATUS"
    systemctl --user status ai-dashboard --no-pager | head -15
fi

# --- Cloudflare tunnel ---
echo "[5/5] Configuring Cloudflare tunnel..."

# Add DNS CNAME
cloudflared tunnel route dns ai-brain dash.YOURDOMAIN.COM 2>&1 | head -3 || \
    echo "      (DNS route: may already exist)"

# Update tunnel ingress via CF API
CF_TOKEN=$(cat "$HOME/.cloudflared/cert.pem" 2>/dev/null | grep -v "BEGIN\|END" | tr -d '\n' | base64 -d 2>/dev/null | \
    python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d.get('apiToken',''))" 2>/dev/null || echo "")

if [ -n "$CF_TOKEN" ]; then
    ACCOUNT_ID="c8ff4f6faf11c14c9db089f162e2fc0c"
    TUNNEL_ID="78a21236-6b6e-4822-8f72-5376afa22ac3"

    # Get existing config and check if dash rule already exists
    EXISTING=$(curl -s -X GET \
        "https://api.cloudflare.com/client/v4/accounts/${ACCOUNT_ID}/cfd_tunnel/${TUNNEL_ID}/configurations" \
        -H "Authorization: Bearer ${CF_TOKEN}" \
        -H "Content-Type: application/json" 2>/dev/null)

    if echo "$EXISTING" | grep -q "dash.YOURDOMAIN.COM"; then
        echo "      Cloudflare tunnel ingress already configured."
    else
        echo "      Adding Cloudflare tunnel ingress rule..."
        echo "      (Run cloudflare-setup.py if needed)"
    fi
else
    echo "      Cloudflare API credentials not available."
fi

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Service status:"
systemctl --user status ai-dashboard --no-pager | grep -E "Active:|Main PID:"
echo ""
echo "Dashboard URLs:"
echo "  Local:  http://127.0.0.1:8200/"
echo "  Public: https://dash.YOURDOMAIN.COM/"
echo ""
echo "Next step: Open https://dash.YOURDOMAIN.COM/ to complete initial setup."
