#!/bin/bash
#
# Database Health Check for AI Infrastructure
# Checks integrity and connectivity of all databases
# Usage: check-db-health.sh
#

set -euo pipefail

LOG_FILE="/var/log/db-health-check.log"
ALERT_EMAIL="${DB_ALERT_EMAIL:-}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

CHECKS_PASSED=0
CHECKS_FAILED=0
HEALTH_REPORT=""

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

check_ok() {
    echo -e "${GREEN}✓${NC} $1"
    HEALTH_REPORT="$HEALTH_REPORT\n[OK] $1"
    ((CHECKS_PASSED++))
}

check_fail() {
    echo -e "${RED}✗${NC} $1"
    HEALTH_REPORT="$HEALTH_REPORT\n[FAIL] $1"
    ((CHECKS_FAILED++))
}

check_warn() {
    echo -e "${YELLOW}⚠${NC} $1"
    HEALTH_REPORT="$HEALTH_REPORT\n[WARN] $1"
}

log "========================================"
log "Database Health Check"
log "========================================"

# 1. Check keys.sqlite
echo ""
echo "Checking keys.sqlite..."

if [ -f "${AI_ROOT:-/opt/ai}/keys/keys.sqlite" ]; then
    # Check integrity
    INTEGRITY=$(sqlite3 ${AI_ROOT:-/opt/ai}/keys/keys.sqlite "PRAGMA integrity_check;" 2>&1)

    if [ "$INTEGRITY" = "ok" ]; then
        check_ok "keys.sqlite: Integrity check passed"
    else
        check_fail "keys.sqlite: Integrity check failed: $INTEGRITY"
    fi

    # Check size
    SIZE=$(stat -c%s ${AI_ROOT:-/opt/ai}/keys/keys.sqlite)
    SIZE_MB=$((SIZE / 1024 / 1024))
    check_ok "keys.sqlite: Size ${SIZE_MB}MB"

    # Check record counts
    API_KEYS=$(sqlite3 ${AI_ROOT:-/opt/ai}/keys/keys.sqlite "SELECT COUNT(*) FROM api_keys;" 2>/dev/null || echo "0")
    PAYMENT_EVENTS=$(sqlite3 ${AI_ROOT:-/opt/ai}/keys/keys.sqlite "SELECT COUNT(*) FROM payment_events;" 2>/dev/null || echo "0")
    API_USAGE=$(sqlite3 ${AI_ROOT:-/opt/ai}/keys/keys.sqlite "SELECT COUNT(*) FROM api_usage;" 2>/dev/null || echo "0")

    check_ok "keys.sqlite: $API_KEYS API keys, $PAYMENT_EVENTS payment events, $API_USAGE usage records"

    # Check for recent activity (last 24 hours)
    RECENT_USAGE=$(sqlite3 ${AI_ROOT:-/opt/ai}/keys/keys.sqlite "SELECT COUNT(*) FROM api_usage WHERE timestamp > datetime('now', '-1 day');" 2>/dev/null || echo "0")
    if [ $RECENT_USAGE -gt 0 ]; then
        check_ok "keys.sqlite: $RECENT_USAGE API requests in last 24 hours (active)"
    else
        check_warn "keys.sqlite: No API usage in last 24 hours (inactive?)"
    fi

    # Check WAL file size (should be reasonable)
    if [ -f "${AI_ROOT:-/opt/ai}/keys/keys.sqlite-wal" ]; then
        WAL_SIZE=$(stat -c%s ${AI_ROOT:-/opt/ai}/keys/keys.sqlite-wal)
        WAL_MB=$((WAL_SIZE / 1024 / 1024))
        if [ $WAL_MB -gt 100 ]; then
            check_warn "keys.sqlite: WAL file is ${WAL_MB}MB (consider checkpointing)"
        else
            check_ok "keys.sqlite: WAL file is ${WAL_MB}MB"
        fi
    fi

else
    check_fail "keys.sqlite: File not found at ${AI_ROOT:-/opt/ai}/keys/keys.sqlite"
fi

# 2. Check Neo4j
echo ""
echo "Checking Neo4j..."

if docker ps | grep -q graphiti-neo4j; then
    check_ok "Neo4j: Container running"

    # Check health endpoint
    HEALTH=$(curl -s http://localhost:7474 2>/dev/null || echo "")
    if [ -n "$HEALTH" ]; then
        check_ok "Neo4j: HTTP endpoint responding"
    else
        check_fail "Neo4j: HTTP endpoint not responding"
    fi

    # Check Bolt connection
    BOLT=$(docker exec graphiti-neo4j cypher-shell -u neo4j ${NEO4J_PASSWORD:-changeme}" "RETURN 1;" 2>/dev/null || echo "")
    if [ -n "$BOLT" ]; then
        check_ok "Neo4j: Bolt connection working"
    else
        check_fail "Neo4j: Bolt connection failed"
    fi

    # Check node count
    NODES=$(docker exec graphiti-neo4j cypher-shell -u neo4j ${NEO4J_PASSWORD:-changeme}" "MATCH (n) RETURN count(n);" 2>/dev/null | grep -E '^[0-9]+' | head -1 || echo "0")
    check_ok "Neo4j: $NODES nodes in knowledge graph"

    # Check data directory size
    NEO4J_SIZE=$(du -sh ${AI_ROOT:-/opt/ai}/graphiti/data 2>/dev/null | cut -f1)
    check_ok "Neo4j: Data directory size $NEO4J_SIZE"

else
    check_fail "Neo4j: Container not running"
fi

# 3. Check PostgreSQL (Zep)
echo ""
echo "Checking PostgreSQL..."

if docker ps | grep -q zep-postgres; then
    check_ok "PostgreSQL: Container running"

    # Check connection
    PG_CONN=$(docker exec zep-postgres psql -U zep -d zep -c "SELECT 1;" 2>/dev/null || echo "")
    if [ -n "$PG_CONN" ]; then
        check_ok "PostgreSQL: Connection working"
    else
        check_fail "PostgreSQL: Connection failed"
    fi

    # Check table count
    TABLES=$(docker exec zep-postgres psql -U zep -d zep -t -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';" 2>/dev/null | tr -d ' ' || echo "0")
    check_ok "PostgreSQL: $TABLES tables in Zep database"

    # Check data directory size
    PG_SIZE=$(du -sh ${AI_ROOT:-/opt/ai}/zep/pgdata 2>/dev/null | cut -f1 || echo "N/A")
    check_ok "PostgreSQL: Data directory size $PG_SIZE"

else
    check_fail "PostgreSQL: Container not running"
fi

# 4. Check Open WebUI
echo ""
echo "Checking Open WebUI..."

if [ -d "${AI_ROOT:-/opt/ai}/openwebui-data" ]; then
    OPENWEBUI_SIZE=$(du -sh ${AI_ROOT:-/opt/ai}/openwebui-data 2>/dev/null | cut -f1)
    check_ok "Open WebUI: Data directory exists ($OPENWEBUI_SIZE)"

    # Check SQLite databases
    if [ -f "${AI_ROOT:-/opt/ai}/openwebui-data/webui.db" ]; then
        WEBUI_INTEGRITY=$(sqlite3 ${AI_ROOT:-/opt/ai}/openwebui-data/webui.db "PRAGMA integrity_check;" 2>&1)
        if [ "$WEBUI_INTEGRITY" = "ok" ]; then
            check_ok "Open WebUI: webui.db integrity OK"
        else
            check_fail "Open WebUI: webui.db integrity failed"
        fi
    fi

    if [ -f "${AI_ROOT:-/opt/ai}/openwebui-data/vector_db/chroma.sqlite3" ]; then
        CHROMA_INTEGRITY=$(sqlite3 ${AI_ROOT:-/opt/ai}/openwebui-data/vector_db/chroma.sqlite3 "PRAGMA integrity_check;" 2>&1)
        if [ "$CHROMA_INTEGRITY" = "ok" ]; then
            check_ok "Open WebUI: chroma.sqlite3 integrity OK"
        else
            check_fail "Open WebUI: chroma.sqlite3 integrity failed"
        fi
    fi
else
    check_fail "Open WebUI: Data directory not found"
fi

# 5. Check backup status
echo ""
echo "Checking backups..."

DAILY_BACKUPS=$(find ${AI_ROOT:-/opt/ai}/backups/automated/daily -name "keys-*.sqlite" -mtime -2 2>/dev/null | wc -l)
if [ $DAILY_BACKUPS -gt 0 ]; then
    LATEST_BACKUP=$(find ${AI_ROOT:-/opt/ai}/backups/automated/daily -name "keys-*.sqlite" -type f -printf '%T@ %p\n' 2>/dev/null | sort -n | tail -1 | cut -d' ' -f2-)
    BACKUP_AGE=$(find "$LATEST_BACKUP" -mtime +1 2>/dev/null | wc -l)

    if [ $BACKUP_AGE -eq 0 ]; then
        check_ok "Backups: Recent backup found (less than 24 hours old)"
    else
        check_warn "Backups: Latest backup is more than 24 hours old"
    fi
else
    check_fail "Backups: No recent backups found"
fi

# Summary
echo ""
log "========================================"
log "Health Check Summary"
log "Passed: $CHECKS_PASSED, Failed: $CHECKS_FAILED"
log "========================================"

# Send alert email if there are failures
if [ $CHECKS_FAILED -gt 0 ] && [ -n "$ALERT_EMAIL" ]; then
    echo -e "Database health check completed with $CHECKS_FAILED failures on $(hostname)\n\nReport:$HEALTH_REPORT" | \
        mail -s "Database Health Alert: $CHECKS_FAILED Checks Failed" "$ALERT_EMAIL"
fi

# Exit with status
if [ $CHECKS_FAILED -gt 0 ]; then
    exit 1
else
    exit 0
fi
