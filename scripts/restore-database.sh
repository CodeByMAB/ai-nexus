#!/bin/bash
#
# Database Restore Script for AI Infrastructure
# Restores databases from backup with safety checks
# Usage: restore-database.sh [keys|neo4j|postgres|openwebui] [backup-file]
#

set -euo pipefail

DB_TYPE="$1"
BACKUP_FILE="$2"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1"
    exit 1
}

warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

# Check if running as root
if [ "$EUID" -ne 0 ]; then
   error "Please run as root"
fi

# Verify backup file exists
if [ ! -f "$BACKUP_FILE" ]; then
    error "Backup file not found: $BACKUP_FILE"
fi

log "========================================"
log "Database Restore"
log "Type: $DB_TYPE"
log "Backup: $BACKUP_FILE"
log "========================================"

# Confirmation prompt
echo ""
echo -e "${YELLOW}WARNING: This will overwrite the current $DB_TYPE database!${NC}"
echo -n "Are you sure you want to continue? (type 'yes' to confirm): "
read -r CONFIRM

if [ "$CONFIRM" != "yes" ]; then
    echo "Restore cancelled."
    exit 0
fi

case "$DB_TYPE" in
    keys)
        log "Restoring keys.sqlite..."

        # Create backup of current database
        CURRENT_DB="${AI_ROOT:-/opt/ai}/keys/keys.sqlite"
        if [ -f "$CURRENT_DB" ]; then
            CURRENT_BACKUP="${AI_ROOT:-/opt/ai}/keys/keys.sqlite.pre-restore-$(date +%Y%m%d-%H%M%S)"
            cp "$CURRENT_DB" "$CURRENT_BACKUP"
            log "Current database backed up to: $CURRENT_BACKUP"
        fi

        # Stop payment webhook
        log "Stopping payment-webhook service..."
        systemctl stop payment-webhook || warning "Could not stop payment-webhook"

        # Restore database
        cp "$BACKUP_FILE" "$CURRENT_DB"
        chown ai:www-data "$CURRENT_DB"
        chmod 600 "$CURRENT_DB"

        # Verify integrity
        sqlite3 "$CURRENT_DB" "PRAGMA integrity_check;" > /dev/null 2>&1
        if [ $? -eq 0 ]; then
            log "Database integrity check passed"
        else
            error "Database integrity check failed - restoring backup"
            cp "$CURRENT_BACKUP" "$CURRENT_DB"
        fi

        # Restart service
        log "Starting payment-webhook service..."
        systemctl start payment-webhook

        log "keys.sqlite restored successfully"
        ;;

    neo4j)
        log "Restoring Neo4j..."

        # Stop Graphiti services
        log "Stopping Graphiti services..."
        cd ${AI_ROOT:-/opt/ai}/graphiti
        docker-compose down

        # Backup current data
        if [ -d "${AI_ROOT:-/opt/ai}/graphiti/data" ]; then
            CURRENT_BACKUP="${AI_ROOT:-/opt/ai}/graphiti/data.pre-restore-$(date +%Y%m%d-%H%M%S)"
            mv ${AI_ROOT:-/opt/ai}/graphiti/data "$CURRENT_BACKUP"
            log "Current data backed up to: $CURRENT_BACKUP"
        fi

        # Extract backup
        mkdir -p ${AI_ROOT:-/opt/ai}/graphiti/data
        tar -xzf "$BACKUP_FILE" -C ${AI_ROOT:-/opt/ai}/graphiti/

        # Fix permissions
        chown -R 7474:7474 ${AI_ROOT:-/opt/ai}/graphiti/data 2>/dev/null || true

        # Start services
        log "Starting Graphiti services..."
        docker-compose up -d

        # Wait for Neo4j to start
        log "Waiting for Neo4j to start..."
        sleep 10

        # Check health
        if docker ps | grep -q graphiti-neo4j; then
            log "Neo4j restored and running"
        else
            error "Neo4j failed to start - check logs"
        fi
        ;;

    postgres)
        log "Restoring PostgreSQL..."

        # Stop Zep
        log "Stopping Zep service..."
        cd ${AI_ROOT:-/opt/ai}/zep
        docker-compose down

        # Start only PostgreSQL
        log "Starting PostgreSQL container..."
        docker-compose up -d zep-postgres
        sleep 5

        # Drop and recreate database
        log "Recreating database..."
        docker exec zep-postgres psql -U zep -d postgres -c "DROP DATABASE IF EXISTS zep;"
        docker exec zep-postgres psql -U zep -d postgres -c "CREATE DATABASE zep;"

        # Restore from backup
        log "Restoring data..."
        gunzip < "$BACKUP_FILE" | docker exec -i zep-postgres psql -U zep -d zep

        if [ $? -eq 0 ]; then
            log "PostgreSQL data restored successfully"
        else
            error "Failed to restore PostgreSQL data"
        fi

        # Start Zep
        log "Starting Zep service..."
        docker-compose up -d zep

        log "PostgreSQL restored successfully"
        ;;

    openwebui)
        log "Restoring Open WebUI..."

        # Stop Open WebUI
        log "Stopping Open WebUI..."
        cd ${AI_ROOT:-/opt/ai}/openwebui
        docker-compose down 2>/dev/null || true

        # Backup current data
        if [ -d "${AI_ROOT:-/opt/ai}/openwebui-data" ]; then
            CURRENT_BACKUP="${AI_ROOT:-/opt/ai}/openwebui-data.pre-restore-$(date +%Y%m%d-%H%M%S)"
            mv ${AI_ROOT:-/opt/ai}/openwebui-data "$CURRENT_BACKUP"
            log "Current data backed up to: $CURRENT_BACKUP"
        fi

        # Extract backup
        tar -xzf "$BACKUP_FILE" -C ${AI_ROOT:-/opt/ai}/

        # Fix permissions
        chown -R root:root ${AI_ROOT:-/opt/ai}/openwebui-data

        # Start Open WebUI
        log "Starting Open WebUI..."
        docker-compose up -d

        log "Open WebUI restored successfully"
        ;;

    *)
        error "Invalid database type: $DB_TYPE (use: keys|neo4j|postgres|openwebui)"
        ;;
esac

log "========================================"
log "Restore completed successfully"
log "========================================"
