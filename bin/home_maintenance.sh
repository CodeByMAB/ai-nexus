#!/bin/bash
# Automated home directory maintenance

HOME_DIR="${HOME}"
LOG_FILE="${HOME_DIR}/.maintenance.log"

log_message() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG_FILE"
}

# Clean up temporary files older than 7 days
find "${HOME_DIR}/tmp" -type f -mtime +7 -delete 2>/dev/null
log_message "Cleaned old temp files"

# Rotate logs older than 30 days
find "${HOME_DIR}/logs" -name "*.log" -mtime +30 -exec gzip {} \; 2>/dev/null
log_message "Rotated old log files"

# Clean up cache directories
find "${HOME_DIR}" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null
find "${HOME_DIR}" -name "*.pyc" -delete 2>/dev/null
log_message "Cleaned Python cache files"

# Maintain directory permissions
chmod 755 "${HOME_DIR}/bin"/* 2>/dev/null
log_message "Updated script permissions"

log_message "Maintenance completed"
