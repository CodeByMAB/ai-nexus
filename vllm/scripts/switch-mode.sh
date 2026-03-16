#!/bin/bash
# Safely switch vLLM mode by updating systemd drop-in and validating health.
#
# Usage:
#   sudo /opt/ai/vllm/scripts/switch-mode.sh [extreme|code|fast|fast+image]
#   sudo /opt/ai/vllm/scripts/switch-mode.sh --mode fast
#   sudo /opt/ai/vllm/scripts/switch-mode.sh --mode code --timeout 360
#   sudo /opt/ai/vllm/scripts/switch-mode.sh --mode fast --no-restart

set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  switch-mode.sh [extreme|code|fast|fast+image]
  switch-mode.sh --mode [extreme|code|fast|fast+image]

Options:
  --no-restart       Only write mode.conf; do not restart services
  --skip-healthcheck Skip post-restart /v1/models validation
  --timeout SECONDS  Healthcheck timeout (default: 240)
  -h, --help         Show help
EOF
}

if [[ "${EUID}" -ne 0 ]]; then
    exec sudo "$0" "$@"
fi

MODE=""
NO_RESTART="false"
SKIP_HEALTHCHECK="false"
TIMEOUT_SECONDS=240

while [[ $# -gt 0 ]]; do
    case "$1" in
        extreme|code|fast|fast+image)
            MODE="$1"
            shift
            ;;
        --mode)
            MODE="${2:-}"
            shift 2
            ;;
        --no-restart)
            NO_RESTART="true"
            shift
            ;;
        --skip-healthcheck)
            SKIP_HEALTHCHECK="true"
            shift
            ;;
        --timeout)
            TIMEOUT_SECONDS="${2:-}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage
            exit 1
            ;;
    esac
done

if [[ -z "$MODE" ]]; then
    MODE="fast"
fi

case "$MODE" in
    extreme|code|fast|fast+image) ;;
    *)
        echo "Invalid mode: $MODE" >&2
        usage
        exit 1
        ;;
esac

if ! [[ "$TIMEOUT_SECONDS" =~ ^[0-9]+$ ]]; then
    echo "Invalid --timeout value: $TIMEOUT_SECONDS" >&2
    exit 1
fi

START_MODEL_SCRIPT="/opt/ai/vllm/scripts/start-model.sh"
MODE_CONF_DIR="/etc/systemd/system/vllm.service.d"
MODE_CONF_PATH="$MODE_CONF_DIR/mode.conf"
BACKUP_PATH="$MODE_CONF_DIR/mode.conf.bak.$(date -u +%Y%m%dT%H%M%SZ)"
HAD_EXISTING_CONF="false"
EXPECTED_MODEL_ID="$MODE"
if [[ "$MODE" == "fast+image" ]]; then
    EXPECTED_MODEL_ID="fast"
fi

wait_for_vllm_health() {
    local mode="$1"
    local timeout="$2"
    local start_ts now_ts response
    start_ts="$(date +%s)"
    while true; do
        now_ts="$(date +%s)"
        if (( now_ts - start_ts > timeout )); then
            return 1
        fi

        if ! systemctl is-active --quiet vllm; then
            sleep 2
            continue
        fi

        response="$(curl -fsS --max-time 5 http://127.0.0.1:11434/v1/models 2>/dev/null || true)"
        if [[ -n "$response" ]] && VLLM_MODELS_JSON="$response" python3 - "$mode" <<'PY'
import json
import os
import sys

mode = sys.argv[1]
try:
    payload = json.loads(os.environ.get("VLLM_MODELS_JSON", ""))
except Exception:
    sys.exit(1)

data = payload.get("data")
if not isinstance(data, list):
    sys.exit(1)

model_ids = {str(item.get("id", "")) for item in data if isinstance(item, dict)}
sys.exit(0 if mode in model_ids else 1)
PY
        then
            return 0
        fi
        sleep 2
    done
}

rollback_mode_conf() {
    if [[ "$HAD_EXISTING_CONF" == "true" && -f "$BACKUP_PATH" ]]; then
        cp -f "$BACKUP_PATH" "$MODE_CONF_PATH"
    elif [[ "$HAD_EXISTING_CONF" == "false" ]]; then
        rm -f "$MODE_CONF_PATH"
    fi
    systemctl daemon-reload || true
    systemctl restart vllm || true
    systemctl restart ai-gateway || true
}

if [[ ! -x "$START_MODEL_SCRIPT" ]]; then
    echo "Missing executable: $START_MODEL_SCRIPT" >&2
    exit 1
fi

EXECSTART_CMD="$("$START_MODEL_SCRIPT" --print-execstart "$MODE")"
if [[ -z "$EXECSTART_CMD" ]]; then
    echo "Failed to generate ExecStart command for mode=$MODE" >&2
    exit 1
fi

mkdir -p "$MODE_CONF_DIR"
if [[ -f "$MODE_CONF_PATH" ]]; then
    HAD_EXISTING_CONF="true"
    cp -f "$MODE_CONF_PATH" "$BACKUP_PATH"
fi

cat > "$MODE_CONF_PATH" <<EOF
[Service]
ExecStart=
ExecStart=$EXECSTART_CMD
EOF

echo "Wrote $MODE_CONF_PATH for mode=$MODE"

if [[ "$NO_RESTART" == "true" ]]; then
    echo "Skipping restart (--no-restart)."
    exit 0
fi

systemctl daemon-reload

if ! systemctl restart vllm; then
    echo "vllm restart failed; rolling back mode.conf" >&2
    rollback_mode_conf
    exit 1
fi

if [[ "$SKIP_HEALTHCHECK" != "true" ]]; then
    echo "Waiting for vLLM healthcheck (mode=$MODE, timeout=${TIMEOUT_SECONDS}s)..."
    if ! wait_for_vllm_health "$EXPECTED_MODEL_ID" "$TIMEOUT_SECONDS"; then
        echo "Healthcheck failed for mode=$MODE; rolling back mode.conf" >&2
        journalctl -u vllm -n 120 --no-pager || true
        rollback_mode_conf
        exit 1
    fi
fi

systemctl restart ai-gateway || true

echo "Mode switch successful: $MODE"
systemctl --no-pager --full status vllm | sed -n '1,24p'
