#!/usr/bin/env bash
# ============================================================
# AI Nexus — Bootstrap Installer
# Run on a fresh Ubuntu 24.04 LTS server:
#   sudo bash install.sh
# ============================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AI_ROOT="${AI_ROOT:-/opt/ai}"
MODELS_ROOT="${MODELS_ROOT:-/opt/models}"
AI_USER="${AI_USER:-ai}"
LOG_FILE="/var/log/ai-nexus-install.log"
MIN_VRAM_GIB=24
MIN_RAM_GIB=32
MIN_DISK_GIB=200

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
log()  { echo -e "${CYAN}[AI-NEXUS]${NC} $*" | tee -a "$LOG_FILE"; }
ok()   { echo -e "${GREEN}[  OK  ]${NC} $*" | tee -a "$LOG_FILE"; }
warn() { echo -e "${YELLOW}[ WARN ]${NC} $*" | tee -a "$LOG_FILE"; }
fail() { echo -e "${RED}[ FAIL ]${NC} $*" | tee -a "$LOG_FILE"; exit 1; }

[[ $EUID -ne 0 ]] && fail "Run as root: sudo bash install.sh"

mkdir -p "$(dirname "$LOG_FILE")"
exec > >(tee -a "$LOG_FILE") 2>&1
echo "=== AI Nexus Install Log $(date -u) ===" >> "$LOG_FILE"

# ---- Hardware check ----
log "Checking hardware requirements..."

RAM_GIB=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
DISK_GIB=$(df -BG / | awk 'NR==2 {gsub("G",""); print $4}')

[[ $RAM_GIB -lt $MIN_RAM_GIB ]] && warn "RAM: ${RAM_GIB}GB detected, ${MIN_RAM_GIB}GB recommended"
[[ $DISK_GIB -lt $MIN_DISK_GIB ]] && warn "Free disk: ${DISK_GIB}GB, ${MIN_DISK_GIB}GB recommended"

if command -v nvidia-smi &>/dev/null; then
  VRAM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 || echo 0)
  VRAM_GIB=$((VRAM / 1024))
  [[ $VRAM_GIB -lt $MIN_VRAM_GIB ]] && warn "GPU VRAM: ${VRAM_GIB}GB, ${MIN_VRAM_GIB}GB recommended for full models"
  ok "GPU detected: $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1) (${VRAM_GIB}GB VRAM)"
else
  warn "No NVIDIA GPU detected. vLLM inference will be unavailable."
fi

ok "RAM: ${RAM_GIB}GB | Disk: ${DISK_GIB}GB free"

# ---- System packages ----
log "Installing system packages..."
apt-get update -qq
apt-get install -y --no-install-recommends \
  git curl wget unzip build-essential \
  python3 python3-pip python3-venv python3-dev \
  nginx certbot python3-certbot-nginx \
  sqlite3 \
  docker.io docker-compose-plugin \
  nodejs npm \
  jq htop tmux \
  ca-certificates gnupg lsb-release

ok "System packages installed"

# ---- NVIDIA Container Toolkit (if GPU present) ----
if command -v nvidia-smi &>/dev/null; then
  log "Installing NVIDIA Container Toolkit..."
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update -qq
  apt-get install -y nvidia-container-toolkit
  nvidia-ctk runtime configure --runtime=docker
  systemctl restart docker
  ok "NVIDIA Container Toolkit installed"
fi

# ---- Create service user ----
log "Creating service user '$AI_USER'..."
if ! id "$AI_USER" &>/dev/null; then
  useradd -m -s /bin/bash -G sudo,docker,adm "$AI_USER"
  ok "User '$AI_USER' created"
else
  usermod -aG docker,adm "$AI_USER"
  ok "User '$AI_USER' already exists — groups updated"
fi

# ---- Directory structure ----
log "Creating directory structure..."
mkdir -p \
  "$AI_ROOT"/{gateway,dashboard/static,payment-webhook,auth,mcp-servers,scripts,keys,backups/{daily,weekly,manual}} \
  "$AI_ROOT/vllm"/{scripts,configs,logs} \
  "$MODELS_ROOT"/{huggingface,llm,invokeai} \
  /var/log/ai-nexus \
  /etc/systemd/system/vllm.service.d

chown -R "$AI_USER:$AI_USER" "$AI_ROOT" "$MODELS_ROOT" /var/log/ai-nexus
ok "Directories created"

# ---- Copy service files ----
log "Copying service files..."
rsync -a --exclude='data/' --exclude='.venv/' --exclude='__pycache__/' \
  "$REPO_ROOT/services/gateway/"  "$AI_ROOT/gateway/"
rsync -a --exclude='data/' --exclude='.venv/' \
  "$REPO_ROOT/services/dashboard/" "$AI_ROOT/dashboard/"
rsync -a --exclude='.venv/' \
  "$REPO_ROOT/services/payment-webhook/" "$AI_ROOT/payment-webhook/"
rsync -a "$REPO_ROOT/services/auth/"  "$AI_ROOT/auth/"
rsync -a "$REPO_ROOT/vllm/"            "$AI_ROOT/vllm/" --exclude='logs/'
rsync -a "$REPO_ROOT/scripts/"         "$AI_ROOT/scripts/"
rsync -a "$REPO_ROOT/bin/"             "/usr/local/bin/" --chmod=755
chmod +x /usr/local/bin/ai /usr/local/bin/gpu_watchdog.sh 2>/dev/null || true

chown -R "$AI_USER:$AI_USER" "$AI_ROOT"
ok "Service files copied"

# ---- .env configuration ----
ENV_FILE="$AI_ROOT/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  log "Configuring environment..."
  cp "$REPO_ROOT/.env.example" "$ENV_FILE"

  read -rp "  Your domain (e.g. example.com): " DOMAIN
  read -rp "  Server IP address: " SERVER_IP
  read -rp "  Stripe secret key (or leave blank): " STRIPE_KEY
  read -rp "  SendGrid API key (or leave blank): " SENDGRID_KEY

  sed -i \
    -e "s/YOURDOMAIN\.COM/$DOMAIN/g" \
    -e "s/YOUR_SERVER_IP/$SERVER_IP/g" \
    -e "s|sk_live_your_stripe_secret_key_here|${STRIPE_KEY:-REPLACE_ME}|" \
    -e "s|SG\.your_sendgrid_api_key_here|${SENDGRID_KEY:-REPLACE_ME}|" \
    "$ENV_FILE"

  # Generate random secrets
  NEO4J_PASS=$(openssl rand -base64 32)
  POSTGRES_PASS=$(openssl rand -base64 32)
  sed -i \
    -e "s|change_this_to_a_strong_random_password_min_32_chars|$NEO4J_PASS|" \
    -e "s|change_this_to_another_strong_random_password|$POSTGRES_PASS|" \
    "$ENV_FILE"

  chmod 600 "$ENV_FILE"
  chown "$AI_USER:$AI_USER" "$ENV_FILE"
  ok ".env configured at $ENV_FILE"
else
  ok ".env already exists — skipping"
fi

# ---- Python venvs ----
log "Creating Python virtual environments..."

for SVC in gateway dashboard payment-webhook auth; do
  SVC_DIR="$AI_ROOT/$SVC"
  [[ -d "$SVC_DIR" ]] || continue
  log "  Setting up $SVC venv..."
  sudo -u "$AI_USER" python3 -m venv "$SVC_DIR/.venv"
  [[ -f "$SVC_DIR/requirements.txt" ]] && \
    sudo -u "$AI_USER" "$SVC_DIR/.venv/bin/pip" install -q -r "$SVC_DIR/requirements.txt"
done

ok "Python environments ready"

# ---- vLLM venv ----
log "Creating vLLM virtual environment..."
sudo -u "$AI_USER" python3 -m venv "$AI_ROOT/vllm/.venv"
sudo -u "$AI_USER" "$AI_ROOT/vllm/.venv/bin/pip" install -q --upgrade pip
sudo -u "$AI_USER" "$AI_ROOT/vllm/.venv/bin/pip" install -q \
  "vllm>=0.15.0" "torch>=2.6.0" "triton>=3.0.0" \
  "mistral-common>=1.8.0" "transformers>=4.51.0"
ok "vLLM environment ready"

# ---- SQLite DB init ----
log "Initializing keys database..."
DB="$AI_ROOT/keys/keys.sqlite"
if [[ ! -f "$DB" ]]; then
  sqlite3 "$DB" << 'SQL'
CREATE TABLE IF NOT EXISTS api_keys (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key_hash TEXT UNIQUE NOT NULL,
  key_prefix TEXT NOT NULL,
  client_name TEXT NOT NULL,
  client_email TEXT,
  plan_type TEXT NOT NULL DEFAULT 'custom',
  monthly_token_allowance INTEGER NOT NULL DEFAULT 500000,
  tokens_used INTEGER DEFAULT 0,
  tokens_remaining INTEGER NOT NULL DEFAULT 500000,
  monthly_tokens_remaining INTEGER DEFAULT 500000,
  purchased_tokens_remaining INTEGER DEFAULT 0,
  tokens_used_this_month INTEGER DEFAULT 0,
  is_active BOOLEAN DEFAULT 1,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  expires_at TEXT,
  subscription_status TEXT DEFAULT 'active',
  notes TEXT
);
CREATE TABLE IF NOT EXISTS token_usage_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key_hash TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  ip_address TEXT,
  tokens_consumed INTEGER NOT NULL DEFAULT 0,
  prompt_tokens INTEGER DEFAULT 0,
  completion_tokens INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS api_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  ts INTEGER NOT NULL
);
SQL
  chown "$AI_USER:$AI_USER" "$DB"
  chmod 640 "$DB"
  ok "Keys database initialized"
else
  ok "Keys database already exists"
fi

# ---- nginx ----
log "Configuring nginx..."
cp "$REPO_ROOT/nginx/"*.conf /etc/nginx/sites-available/ 2>/dev/null || true
source "$ENV_FILE" 2>/dev/null || true
for conf in /etc/nginx/sites-available/*.conf; do
  name=$(basename "$conf")
  ln -sf "$conf" "/etc/nginx/sites-enabled/$name" 2>/dev/null || true
  sed -i "s/YOURDOMAIN\.COM/${API_DOMAIN:-localhost}/g" "$conf"
done
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
ok "nginx configured"

# ---- systemd services ----
log "Installing systemd services..."
for svc_file in "$REPO_ROOT/systemd/"*.service; do
  svc_name=$(basename "$svc_file")
  dest="/etc/systemd/system/$svc_name"
  cp "$svc_file" "$dest"
  sed -i \
    -e "s|\${AI_ROOT:-/opt/ai}|$AI_ROOT|g" \
    -e "s|User=ai|User=$AI_USER|g" \
    -e "s|Group=ai|Group=$AI_USER|g" \
    "$dest"
done
systemctl daemon-reload
ok "Systemd services installed"

# ---- sudoers for service control ----
SYSTEMCTL=$(which systemctl)
cat > /etc/sudoers.d/ai-nexus << EOF
# AI Nexus — service control
$AI_USER ALL=(ALL) NOPASSWD: $SYSTEMCTL start vllm
$AI_USER ALL=(ALL) NOPASSWD: $SYSTEMCTL stop vllm
$AI_USER ALL=(ALL) NOPASSWD: $SYSTEMCTL restart vllm
$AI_USER ALL=(ALL) NOPASSWD: $SYSTEMCTL start ai-gateway
$AI_USER ALL=(ALL) NOPASSWD: $SYSTEMCTL stop ai-gateway
$AI_USER ALL=(ALL) NOPASSWD: $SYSTEMCTL restart ai-gateway
$AI_USER ALL=(ALL) NOPASSWD: $SYSTEMCTL start payment-webhook
$AI_USER ALL=(ALL) NOPASSWD: $SYSTEMCTL stop payment-webhook
$AI_USER ALL=(ALL) NOPASSWD: $SYSTEMCTL restart payment-webhook
$AI_USER ALL=(ALL) NOPASSWD: $AI_ROOT/vllm/scripts/switch-mode.sh
EOF
chmod 440 /etc/sudoers.d/ai-nexus
ok "sudoers configured"

# ---- Docker services ----
log "Starting Docker services..."
cd "$REPO_ROOT"
docker compose pull --quiet 2>/dev/null || true
docker compose up -d --remove-orphans
ok "Docker services started"

# ---- Start systemd services ----
log "Enabling and starting systemd services..."
for svc in ai-gateway payment-webhook ai-dashboard; do
  systemctl enable "$svc" 2>/dev/null || true
  systemctl start "$svc" 2>/dev/null || true
done
ok "Services started"

# ---- Health check ----
log "Running health checks..."
sleep 5
PASS=0; FAIL=0
check() {
  local name=$1; local cmd=$2
  if eval "$cmd" &>/dev/null; then
    ok "  $name ✓"
    ((PASS++))
  else
    warn "  $name ✗ (may still be starting)"
    ((FAIL++))
  fi
}

check "nginx"        "systemctl is-active --quiet nginx"
check "docker"       "docker ps &>/dev/null"
check "openwebui"    "docker inspect openwebui &>/dev/null"
check "ai-gateway"   "curl -sf http://localhost:5050/health"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  AI Nexus installation complete!"
echo "  Passed: $PASS | Warnings: $FAIL"
echo ""
echo "  Next steps:"
echo "  1. Edit $ENV_FILE with your credentials"
echo "  2. Download models: huggingface-cli download mistralai/Ministral-3-14B-Instruct-2512"
echo "  3. Start vLLM: ai mode fast"
echo "  4. Open dashboard: https://${DASHBOARD_DOMAIN:-YOUR_DOMAIN}/dash"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
