#!/bin/bash
# Run once with sudo to grant the dashboard passwordless control of AI services.
# Usage: sudo bash /opt/ai/dashboard/setup-sudoers.sh

set -e
SUDOERS_FILE=/etc/sudoers.d/ai-dashboard
SYSTEMCTL=$(which systemctl)

cat > "$SUDOERS_FILE" << EOF
# AI Dashboard — passwordless service control
ai ALL=(ALL) NOPASSWD: $SYSTEMCTL start vllm
ai ALL=(ALL) NOPASSWD: $SYSTEMCTL stop vllm
ai ALL=(ALL) NOPASSWD: $SYSTEMCTL restart vllm
ai ALL=(ALL) NOPASSWD: $SYSTEMCTL start ai-gateway
ai ALL=(ALL) NOPASSWD: $SYSTEMCTL stop ai-gateway
ai ALL=(ALL) NOPASSWD: $SYSTEMCTL restart ai-gateway
ai ALL=(ALL) NOPASSWD: $SYSTEMCTL start openai-shim
ai ALL=(ALL) NOPASSWD: $SYSTEMCTL stop openai-shim
ai ALL=(ALL) NOPASSWD: $SYSTEMCTL restart openai-shim
ai ALL=(ALL) NOPASSWD: $SYSTEMCTL start payment-webhook
ai ALL=(ALL) NOPASSWD: $SYSTEMCTL stop payment-webhook
ai ALL=(ALL) NOPASSWD: $SYSTEMCTL restart payment-webhook
ai ALL=(ALL) NOPASSWD: /opt/ai/vllm/scripts/switch-mode.sh
EOF

chmod 440 "$SUDOERS_FILE"
visudo -c && echo "✓ Sudoers configured — service controls are now active in the dashboard."
