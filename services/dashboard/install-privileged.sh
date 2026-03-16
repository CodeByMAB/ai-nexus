#!/bin/bash
# AI Dashboard - Privileged Installation Script
# Must be run as root (or with sudo)
# Usage: sudo bash /opt/ai/dashboard/install-privileged.sh

set -euo pipefail

if [[ "$(id -u)" != "0" ]]; then
    echo "Error: This script must be run as root (sudo bash $0)"
    exit 1
fi

echo "=== AI Dashboard Privileged Install ==="

# Copy systemd service
echo "[1/4] Installing systemd service..."
cp /tmp/ai-dashboard.service /etc/systemd/system/ai-dashboard.service
chmod 644 /etc/systemd/system/ai-dashboard.service
systemctl daemon-reload
systemctl enable ai-dashboard
echo "      Service enabled."

# Configure nginx
echo "[2/4] Configuring nginx..."
cp /tmp/dashboard.conf /etc/nginx/sites-available/dashboard.conf
chmod 644 /etc/nginx/sites-available/dashboard.conf
if [ ! -L /etc/nginx/sites-enabled/dashboard.conf ]; then
    ln -s /etc/nginx/sites-available/dashboard.conf /etc/nginx/sites-enabled/dashboard.conf
    echo "      Created symlink in sites-enabled."
fi
nginx -t
nginx -s reload
echo "      Nginx configured and reloaded."

# Fix data dir permissions
echo "[3/4] Setting data directory permissions..."
chown -R ai:ai /opt/ai/dashboard
chmod 750 /opt/ai/dashboard/data

# Start service
echo "[4/4] Starting ai-dashboard service..."
systemctl start ai-dashboard
sleep 2
systemctl status ai-dashboard --no-pager | head -15

echo ""
echo "=== Privileged install complete ==="
echo "Dashboard running at http://127.0.0.1:8200/"
echo "Via Cloudflare: https://dash.YOURDOMAIN.COM/"
