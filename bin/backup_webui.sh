#!/bin/bash
##############################################################################
# Open WebUI Backup Script
#
# This script backs up the Open WebUI database and data directory.
# It keeps the last 7 daily backups to save disk space.
#
# Usage: ./backup_webui.sh [--full]
#   --full: Backup entire data directory (default: database only)
##############################################################################

set -euo pipefail

# Configuration
BACKUP_DIR="/opt/backups/openwebui"
DATA_DIR="/opt/openwebui/data"
DB_FILE="$DATA_DIR/webui.db"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RETENTION_DAYS=7

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if running as root or with sudo
if [[ $EUID -ne 0 ]]; then
   log_error "This script must be run as root or with sudo"
   exit 1
fi

# Create backup directory if it doesn't exist
mkdir -p "$BACKUP_DIR"

# Parse arguments
FULL_BACKUP=false
if [[ "${1:-}" == "--full" ]]; then
    FULL_BACKUP=true
fi

# Check if database exists
if [[ ! -f "$DB_FILE" ]]; then
    log_error "Database file not found: $DB_FILE"
    exit 1
fi

log_info "Starting backup at $(date)"

# Get database size and stats
DB_SIZE=$(du -h "$DB_FILE" | cut -f1)
USER_COUNT=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM user;" 2>/dev/null || echo "unknown")
CHAT_COUNT=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM chat;" 2>/dev/null || echo "unknown")

log_info "Database size: $DB_SIZE"
log_info "Users: $USER_COUNT, Chats: $CHAT_COUNT"

if $FULL_BACKUP; then
    # Full backup of entire data directory
    BACKUP_FILE="$BACKUP_DIR/openwebui_full_${TIMESTAMP}.tar.gz"
    log_info "Creating full backup: $BACKUP_FILE"

    tar -czf "$BACKUP_FILE" -C "$(dirname "$DATA_DIR")" "$(basename "$DATA_DIR")"

    BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
    log_success "Full backup created: $BACKUP_SIZE"
else
    # Database-only backup
    BACKUP_FILE="$BACKUP_DIR/webui_db_${TIMESTAMP}.db.gz"
    log_info "Creating database backup: $BACKUP_FILE"

    # Use sqlite3 to create a clean backup (handles open connections)
    sqlite3 "$DB_FILE" ".backup '$BACKUP_DIR/webui_db_${TIMESTAMP}.db'"
    gzip "$BACKUP_DIR/webui_db_${TIMESTAMP}.db"

    BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
    log_success "Database backup created: $BACKUP_SIZE"
fi

# Clean up old backups (keep last RETENTION_DAYS backups)
log_info "Cleaning up old backups (keeping last $RETENTION_DAYS days)"
find "$BACKUP_DIR" -name "*.gz" -type f -mtime +$RETENTION_DAYS -delete

# List recent backups
BACKUP_COUNT=$(find "$BACKUP_DIR" -name "*.gz" -type f | wc -l)
log_info "Total backups: $BACKUP_COUNT"

# Calculate total backup size
TOTAL_SIZE=$(du -sh "$BACKUP_DIR" | cut -f1)
log_info "Total backup directory size: $TOTAL_SIZE"

log_success "Backup completed successfully at $(date)"

# Output backup file path for automation
echo "$BACKUP_FILE"
