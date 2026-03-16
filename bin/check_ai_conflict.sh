#!/bin/bash

# AI Services Conflict Checker
# Returns 1 if there are conflicting services, 0 if safe to start

# Check if user-level ai-services is running
if systemctl --user is-active ai-services >/dev/null 2>&1; then
    echo "User-level ai-services is running, system service should not start"
    exit 1
fi

# Check for multiple ai manager processes (allow one for the systemd service)
AI_PROCESSES=$(pgrep -f "${HOME}/bin/ai" | wc -l)
if [ "$AI_PROCESSES" -gt 1 ]; then
    echo "Multiple AI manager processes detected ($AI_PROCESSES), avoiding conflict"
    exit 1
fi

# Check if individual services are already bound to their ports
if lsof -i :5001 >/dev/null 2>&1; then
    echo "Port 5001 (KoboldCpp) already in use"
    exit 1
fi

if lsof -i :7860 >/dev/null 2>&1; then
    echo "Port 7860 (Stable Diffusion) already in use"
    exit 1
fi

if lsof -i :5678 >/dev/null 2>&1; then
    echo "Port 5678 (n8n) already in use"
    exit 1
fi

echo "No conflicts detected, safe to start AI services"
exit 0