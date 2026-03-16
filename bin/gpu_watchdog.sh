#!/bin/bash
# GPU Watchdog - Monitor GPU power and auto-restart Ollama if dangerously high
# This prevents overheating when requests timeout but Ollama keeps processing

POWER_THRESHOLD=280  # Watts - restart if sustained above this
TIME_THRESHOLD=120   # Seconds - how long to tolerate high power
CHECK_INTERVAL=5     # Seconds between checks
LOG_FILE="$HOME/logs/gpu_watchdog.log"
MAX_LOG_SIZE=1048576  # 1MB in bytes - rotate when log exceeds this

high_power_start=0
currently_high=false

# Function to rotate log if too large
rotate_log_if_needed() {
    if [[ -f "$LOG_FILE" ]]; then
        local size=$(stat -f%z "$LOG_FILE" 2>/dev/null || stat -c%s "$LOG_FILE" 2>/dev/null)
        if [[ -n "$size" ]] && (( size > MAX_LOG_SIZE )); then
            # Keep last 100 lines, discard the rest
            tail -100 "$LOG_FILE" > "${LOG_FILE}.tmp"
            mv "${LOG_FILE}.tmp" "$LOG_FILE"
            echo "$(date '+%Y-%m-%d %H:%M:%S') - Log rotated (was ${size} bytes, kept last 100 lines)"
        fi
    fi
}

echo "GPU Watchdog started - monitoring for sustained power > ${POWER_THRESHOLD}W for > ${TIME_THRESHOLD}s"
echo "Log file: $LOG_FILE (auto-rotates at 1MB, keeps last 100 lines)"

while true; do
    # Rotate log if needed (check every iteration)
    rotate_log_if_needed
    # Get current GPU power draw
    power=$(nvidia-smi --query-gpu=power.draw --format=csv,noheader,nounits 2>/dev/null | awk '{print int($1)}')

    if [[ -z "$power" ]]; then
        echo "Warning: Could not read GPU power"
        sleep $CHECK_INTERVAL
        continue
    fi

    if (( power > POWER_THRESHOLD )); then
        if [[ "$currently_high" == "false" ]]; then
            # Just crossed threshold
            high_power_start=$(date +%s)
            currently_high=true
            echo "⚠️  GPU power high: ${power}W (threshold: ${POWER_THRESHOLD}W) - monitoring..."
        else
            # Still high - check duration
            now=$(date +%s)
            duration=$((now - high_power_start))

            if (( duration > TIME_THRESHOLD )); then
                echo "🚨 DANGER: GPU power ${power}W sustained for ${duration}s - RESTARTING OLLAMA"
                sudo systemctl restart ollama
                echo "✓ Ollama restarted - GPU should cool down"
                # Reset
                currently_high=false
                high_power_start=0
                # Wait a bit for restart
                sleep 10
            else
                echo "⚠️  GPU power ${power}W for ${duration}s (limit: ${TIME_THRESHOLD}s)"
            fi
        fi
    else
        if [[ "$currently_high" == "true" ]]; then
            echo "✓ GPU power normalized: ${power}W"
        fi
        currently_high=false
        high_power_start=0
    fi

    sleep $CHECK_INTERVAL
done
