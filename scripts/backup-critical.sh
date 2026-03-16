#!/bin/bash
#
# Critical Database Backup Script for AI Infrastructure
# Backs up: keys.sqlite, Neo4j, PostgreSQL, Open WebUI
# Usage: backup-critical.sh [daily|weekly|pre-update]
#

set -euo pipefail

# Configuration
BACKUP_TYPE="${1:-daily}"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_BASE="${AI_ROOT:-/opt/ai}/backups/automated"
LOG_FILE="/var/log/ai-backup.log"
ALERT_EMAIL="${BACKUP_ALERT_EMAIL:-}"

# Backup retention (days)
DAILY_RETENTION=7
WEEKLY_RETENTION=28
PREUPDATE_RETENTION=90

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Logging function
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1" | tee -a "$LOG_FILE"
}

success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1" | tee -a "$LOG_FILE"
}

warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1" | tee -a "$LOG_FILE"
}

# Check if running as root
if [ "$EUID" -ne 0 ]; then
   error "Please run as root"
   exit 1
fi

# Determine backup directory
case "$BACKUP_TYPE" in
    daily)
        BACKUP_DIR="$BACKUP_BASE/daily"
        RETENTION=$DAILY_RETENTION
        ;;
    weekly)
        BACKUP_DIR="$BACKUP_BASE/weekly"
        RETENTION=$WEEKLY_RETENTION
        ;;
    pre-update)
        BACKUP_DIR="$BACKUP_BASE/manual/pre-update-$TIMESTAMP"
        RETENTION=$PREUPDATE_RETENTION
        ;;
    *)
        error "Invalid backup type: $BACKUP_TYPE (use: daily|weekly|pre-update)"
        exit 1
        ;;
esac

log "========================================"
log "Starting $BACKUP_TYPE backup: $TIMESTAMP"
log "Backup directory: $BACKUP_DIR"
log "========================================"

# Create backup directory
mkdir -p "$BACKUP_DIR"
cd "$BACKUP_DIR"

# Initialize manifest
MANIFEST="manifest-$TIMESTAMP.json"
echo "{" > "$MANIFEST"
echo "  \"backup_type\": \"$BACKUP_TYPE\"," >> "$MANIFEST"
echo "  \"timestamp\": \"$TIMESTAMP\"," >> "$MANIFEST"
echo "  \"hostname\": \"$(hostname)\"," >> "$MANIFEST"
echo "  \"databases\": {" >> "$MANIFEST"

BACKUP_SUCCESS=0
BACKUP_FAILED=0

# Function to add to manifest
add_to_manifest() {
    local name="$1"
    local file="$2"
    local status="$3"

    if [ -f "$file" ]; then
        local size=$(stat -c%s "$file")
        local checksum=$(sha256sum "$file" | cut -d' ' -f1)
        echo "    \"$name\": {" >> "$MANIFEST"
        echo "      \"file\": \"$file\"," >> "$MANIFEST"
        echo "      \"size\": $size," >> "$MANIFEST"
        echo "      \"checksum\": \"$checksum\"," >> "$MANIFEST"
        echo "      \"status\": \"$status\"" >> "$MANIFEST"
        echo "    }," >> "$MANIFEST"
    else
        echo "    \"$name\": {" >> "$MANIFEST"
        echo "      \"status\": \"$status\"," >> "$MANIFEST"
        echo "      \"error\": \"File not found\"" >> "$MANIFEST"
        echo "    }," >> "$MANIFEST"
    fi
}

# 1. Backup keys.sqlite (Auth Database)
log "Backing up keys.sqlite..."
KEYS_DB="${AI_ROOT:-/opt/ai}/keys/keys.sqlite"
KEYS_BACKUP="keys-$TIMESTAMP.sqlite"

if [ -f "$KEYS_DB" ]; then
    # Use SQLite's backup command for safe hot backup
    sqlite3 "$KEYS_DB" ".backup '$BACKUP_DIR/$KEYS_BACKUP'"

    if [ $? -eq 0 ] && [ -f "$KEYS_BACKUP" ]; then
        # Verify backup integrity
        sqlite3 "$KEYS_BACKUP" "PRAGMA integrity_check;" > /dev/null 2>&1

        if [ $? -eq 0 ]; then
            chmod 600 "$KEYS_BACKUP"
            chown root:www-data "$KEYS_BACKUP"
            success "keys.sqlite backed up successfully ($(du -h "$KEYS_BACKUP" | cut -f1))"
            add_to_manifest "keys_sqlite" "$KEYS_BACKUP" "success"
            ((BACKUP_SUCCESS++))
        else
            error "keys.sqlite backup verification failed"
            add_to_manifest "keys_sqlite" "$KEYS_BACKUP" "verification_failed"
            ((BACKUP_FAILED++))
        fi
    else
        error "Failed to create keys.sqlite backup"
        add_to_manifest "keys_sqlite" "" "backup_failed"
        ((BACKUP_FAILED++))
    fi
else
    warning "keys.sqlite not found at $KEYS_DB"
    add_to_manifest "keys_sqlite" "" "not_found"
fi

# 2. Backup Neo4j (Graphiti Knowledge Graph)
log "Backing up Neo4j data..."
NEO4J_DATA="${AI_ROOT:-/opt/ai}/graphiti/data"
NEO4J_BACKUP="neo4j-$TIMESTAMP.tar.gz"

if [ -d "$NEO4J_DATA" ]; then
    # Check if Neo4j container is running
    if docker ps | grep -q graphiti-neo4j; then
        # Use Neo4j's dump command for consistent backup
        docker exec graphiti-neo4j neo4j-admin database dump neo4j --to-path=/data/backups 2>/dev/null || true
    fi

    # Tar the entire data directory
    tar -czf "$BACKUP_DIR/$NEO4J_BACKUP" -C ${AI_ROOT:-/opt/ai}/graphiti data/ 2>/dev/null

    if [ $? -eq 0 ] && [ -f "$NEO4J_BACKUP" ]; then
        chmod 640 "$NEO4J_BACKUP"
        chown root:www-data "$NEO4J_BACKUP"
        success "Neo4j backed up successfully ($(du -h "$NEO4J_BACKUP" | cut -f1))"
        add_to_manifest "neo4j" "$NEO4J_BACKUP" "success"
        ((BACKUP_SUCCESS++))
    else
        error "Failed to create Neo4j backup"
        add_to_manifest "neo4j" "" "backup_failed"
        ((BACKUP_FAILED++))
    fi
else
    warning "Neo4j data not found at $NEO4J_DATA"
    add_to_manifest "neo4j" "" "not_found"
fi

# 3. Backup PostgreSQL (Zep Agent Memory)
log "Backing up PostgreSQL..."
POSTGRES_BACKUP="postgres-$TIMESTAMP.sql.gz"

if docker ps | grep -q zep-postgres; then
    # Use pg_dump for consistent backup
    docker exec zep-postgres pg_dump -U zep -d zep | gzip > "$BACKUP_DIR/$POSTGRES_BACKUP"

    if [ $? -eq 0 ] && [ -f "$POSTGRES_BACKUP" ]; then
        chmod 640 "$POSTGRES_BACKUP"
        chown root:www-data "$POSTGRES_BACKUP"
        success "PostgreSQL backed up successfully ($(du -h "$POSTGRES_BACKUP" | cut -f1))"
        add_to_manifest "postgresql" "$POSTGRES_BACKUP" "success"
        ((BACKUP_SUCCESS++))
    else
        error "Failed to create PostgreSQL backup"
        add_to_manifest "postgresql" "" "backup_failed"
        ((BACKUP_FAILED++))
    fi
else
    warning "PostgreSQL container not running"
    add_to_manifest "postgresql" "" "container_not_running"
fi

# 4. Backup Open WebUI Database
log "Backing up Open WebUI..."
OPENWEBUI_DATA="${AI_ROOT:-/opt/ai}/openwebui-data"
OPENWEBUI_BACKUP="openwebui-$TIMESTAMP.tar.gz"

if [ -d "$OPENWEBUI_DATA" ]; then
    tar -czf "$BACKUP_DIR/$OPENWEBUI_BACKUP" -C ${AI_ROOT:-/opt/ai} openwebui-data/ 2>/dev/null

    if [ $? -eq 0 ] && [ -f "$OPENWEBUI_BACKUP" ]; then
        chmod 640 "$OPENWEBUI_BACKUP"
        chown root:www-data "$OPENWEBUI_BACKUP"
        success "Open WebUI backed up successfully ($(du -h "$OPENWEBUI_BACKUP" | cut -f1))"
        add_to_manifest "openwebui" "$OPENWEBUI_BACKUP" "success"
        ((BACKUP_SUCCESS++))
    else
        error "Failed to create Open WebUI backup"
        add_to_manifest "openwebui" "" "backup_failed"
        ((BACKUP_FAILED++))
    fi
else
    warning "Open WebUI data not found at $OPENWEBUI_DATA"
    add_to_manifest "openwebui" "" "not_found"
fi

# 5. Backup service configurations (weekly and pre-update only)
if [ "$BACKUP_TYPE" = "weekly" ] || [ "$BACKUP_TYPE" = "pre-update" ]; then
    log "Backing up service configurations..."
    CONFIG_BACKUP="configs-$TIMESTAMP.tar.gz"

    tar -czf "$BACKUP_DIR/$CONFIG_BACKUP" \
        -C ${AI_ROOT:-/opt/ai} \
        --exclude='*.log' \
        --exclude='data' \
        --exclude='logs' \
        --exclude='pgdata' \
        --exclude='node_modules' \
        --exclude='__pycache__' \
        gateway/ \
        payment-webhook/ \
        graphiti/ \
        zep/ \
        mcp-servers/ \
        2>/dev/null

    if [ $? -eq 0 ] && [ -f "$CONFIG_BACKUP" ]; then
        chmod 640 "$CONFIG_BACKUP"
        chown root:www-data "$CONFIG_BACKUP"
        success "Configurations backed up successfully ($(du -h "$CONFIG_BACKUP" | cut -f1))"
        add_to_manifest "configs" "$CONFIG_BACKUP" "success"
        ((BACKUP_SUCCESS++))
    fi
fi

# Close manifest JSON
echo "    \"__end__\": {}" >> "$MANIFEST"
echo "  }," >> "$MANIFEST"
echo "  \"summary\": {" >> "$MANIFEST"
echo "    \"successful\": $BACKUP_SUCCESS," >> "$MANIFEST"
echo "    \"failed\": $BACKUP_FAILED," >> "$MANIFEST"
echo "    \"total_size\": \"$(du -sh "$BACKUP_DIR" | cut -f1)\"" >> "$MANIFEST"
echo "  }" >> "$MANIFEST"
echo "}" >> "$MANIFEST"

# Cleanup old backups based on retention
if [ "$BACKUP_TYPE" != "pre-update" ]; then
    log "Cleaning up old $BACKUP_TYPE backups (retention: $RETENTION days)..."
    find "$BACKUP_BASE/$BACKUP_TYPE" -type f -mtime +$RETENTION -delete 2>/dev/null || true
    CLEANED=$(find "$BACKUP_BASE/$BACKUP_TYPE" -type f -mtime +$RETENTION 2>/dev/null | wc -l)
    if [ "$CLEANED" -gt 0 ]; then
        log "Cleaned up $CLEANED old backup files"
    fi
fi

# Summary
log "========================================"
log "Backup completed: $BACKUP_SUCCESS successful, $BACKUP_FAILED failed"
log "Total backup size: $(du -sh "$BACKUP_DIR" | cut -f1)"
log "Manifest: $BACKUP_DIR/$MANIFEST"
log "========================================"

# Send alert email if configured and there were failures
if [ -n "$ALERT_EMAIL" ] && [ $BACKUP_FAILED -gt 0 ]; then
    echo "AI Infrastructure backup completed with $BACKUP_FAILED failures on $(hostname)" | \
        mail -s "Backup Alert: Failures Detected" "$ALERT_EMAIL"
fi

# Exit with error if any backups failed
if [ $BACKUP_FAILED -gt 0 ]; then
    exit 1
else
    exit 0
fi
