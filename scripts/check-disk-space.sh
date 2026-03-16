#!/bin/bash
#
# Disk Space Monitoring for AI Infrastructure
# Checks disk usage and alerts if thresholds exceeded
# Usage: check-disk-space.sh
#

set -euo pipefail

# Configuration
ALERT_EMAIL="${DISK_ALERT_EMAIL:-}"
LOG_FILE="/var/log/disk-space-check.log"

# Thresholds (percentage)
MAIN_DRIVE_WARNING=75
MAIN_DRIVE_CRITICAL=85
MODELS_WARNING=85
MODELS_CRITICAL=95

# Alert tracking (to avoid spam)
ALERT_FILE="/var/run/disk-alert.last"
ALERT_COOLDOWN=3600  # 1 hour between alerts

# Get current usage
MAIN_USAGE=$(df / | awk 'NR==2 {print $5}' | sed 's/%//')
MODELS_USAGE=$(df ${MODELS_ROOT:-/opt/models} | awk 'NR==2 {print $5}' | sed 's/%//')

# Get sizes for context
MAIN_SIZE=$(df -h / | awk 'NR==2 {print $2}')
MAIN_USED=$(df -h / | awk 'NR==2 {print $3}')
MAIN_AVAIL=$(df -h / | awk 'NR==2 {print $4}')

MODELS_SIZE=$(df -h ${MODELS_ROOT:-/opt/models} | awk 'NR==2 {print $2}')
MODELS_USED=$(df -h ${MODELS_ROOT:-/opt/models} | awk 'NR==2 {print $3}')
MODELS_AVAIL=$(df -h ${MODELS_ROOT:-/opt/models} | awk 'NR==2 {print $4}')

# Database size check
DB_SIZE=$(du -sh ${AI_ROOT:-/opt/ai}/keys/keys.sqlite 2>/dev/null | cut -f1 || echo "N/A")
NEO4J_SIZE=$(du -sh ${AI_ROOT:-/opt/ai}/graphiti/data 2>/dev/null | cut -f1 || echo "N/A")
OPENWEBUI_SIZE=$(du -sh ${AI_ROOT:-/opt/ai}/openwebui-data 2>/dev/null | cut -f1 || echo "N/A")

# Check alert cooldown
should_alert() {
    if [ ! -f "$ALERT_FILE" ]; then
        return 0  # No previous alert, should alert
    fi

    LAST_ALERT=$(stat -c %Y "$ALERT_FILE")
    NOW=$(date +%s)
    ELAPSED=$((NOW - LAST_ALERT))

    if [ $ELAPSED -gt $ALERT_COOLDOWN ]; then
        return 0  # Cooldown expired, should alert
    else
        return 1  # Still in cooldown
    fi
}

# Send alert function
send_alert() {
    local LEVEL="$1"
    local MESSAGE="$2"

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$LEVEL] $MESSAGE" >> "$LOG_FILE"

    if [ -n "$ALERT_EMAIL" ] && should_alert; then
        echo "$MESSAGE" | mail -s "Disk Space Alert [$LEVEL]: AI Infrastructure" "$ALERT_EMAIL"
        touch "$ALERT_FILE"
    fi
}

# Check main drive
if [ $MAIN_USAGE -ge $MAIN_DRIVE_CRITICAL ]; then
    send_alert "CRITICAL" "Main drive at CRITICAL level: ${MAIN_USAGE}% used (${MAIN_USED}/${MAIN_SIZE}, ${MAIN_AVAIL} free)

Databases:
- keys.sqlite: $DB_SIZE
- Neo4j: $NEO4J_SIZE
- Open WebUI: $OPENWEBUI_SIZE

Action required:
1. Review /var/log files for cleanup
2. Check /tmp for large temporary files
3. Consider offloading more data to ${MODELS_ROOT:-/opt/models}
4. Review backup retention policies

Current space breakdown:
$(df -h / | tail -1)"

elif [ $MAIN_USAGE -ge $MAIN_DRIVE_WARNING ]; then
    send_alert "WARNING" "Main drive approaching capacity: ${MAIN_USAGE}% used (${MAIN_USED}/${MAIN_SIZE}, ${MAIN_AVAIL} free)

Monitor this situation. Consider cleanup if it grows further."
fi

# Check models partition
if [ $MODELS_USAGE -ge $MODELS_CRITICAL ]; then
    send_alert "CRITICAL" "Models partition at CRITICAL level: ${MODELS_USAGE}% used (${MODELS_USED}/${MODELS_SIZE}, ${MODELS_AVAIL} free)

Action required:
1. Review model files in ${MODELS_ROOT:-/opt/models}
2. Remove unused models
3. Consider archiving old models
4. Check for duplicate files

Current space breakdown:
$(df -h ${MODELS_ROOT:-/opt/models} | tail -1)"

elif [ $MODELS_USAGE -ge $MODELS_WARNING ]; then
    send_alert "WARNING" "Models partition approaching capacity: ${MODELS_USAGE}% used (${MODELS_USED}/${MODELS_SIZE}, ${MODELS_AVAIL} free)

Monitor this situation."
fi

# Database growth check (alert if keys.sqlite > 100MB)
if [ -f "${AI_ROOT:-/opt/ai}/keys/keys.sqlite" ]; then
    DB_SIZE_BYTES=$(stat -c%s "${AI_ROOT:-/opt/ai}/keys/keys.sqlite")
    if [ $DB_SIZE_BYTES -gt 104857600 ]; then  # 100MB
        send_alert "INFO" "keys.sqlite has grown to $DB_SIZE. Consider archiving old payment_events and api_usage records."
    fi
fi

# Log status (for debugging, even if no alerts)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Status: Main ${MAIN_USAGE}% (${MAIN_AVAIL} free), Models ${MODELS_USAGE}% (${MODELS_AVAIL} free)" >> "$LOG_FILE"

# Exit with status based on most severe condition
if [ $MAIN_USAGE -ge $MAIN_DRIVE_CRITICAL ] || [ $MODELS_USAGE -ge $MODELS_CRITICAL ]; then
    exit 2  # Critical
elif [ $MAIN_USAGE -ge $MAIN_DRIVE_WARNING ] || [ $MODELS_USAGE -ge $MODELS_WARNING ]; then
    exit 1  # Warning
else
    exit 0  # OK
fi
