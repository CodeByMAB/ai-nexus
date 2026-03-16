#!/bin/bash
# Stable Diffusion WebUI Startup Monitor
# Enhanced monitoring script with progress tracking and visual indicators

ELAPSED_SECONDS=0
ANIMATION=("⠋" "⠙" "⠹" "⠸" "⠼" "⠴" "⠦" "⠧" "⠇" "⠏")
FRAME=0

# Function to show animated progress bar
show_progress() {
    local total=$1
    local current=$2
    local desc=$3
    local width=30
    
    if [ $total -eq 0 ]; then
        echo "$desc: ${ANIMATION[$FRAME]} Working..."
        return
    fi
    
    local filled=$(( current * width / total ))
    local empty=$(( width - filled ))
    
    printf "$desc: ["
    printf "%*s" $filled | tr ' ' '█'
    printf "%*s" $empty | tr ' ' '░'
    printf "] %d/%d\n" $current $total
}

while true; do
    clear
    echo "╔══════════════════════════════════════════════════════════════════╗"
    echo "║           🎨 Stable Diffusion WebUI Startup Monitor             ║"
    echo "╚══════════════════════════════════════════════════════════════════╝"
    echo ""
    ELAPSED_MINUTES=$((ELAPSED_SECONDS / 60))
    ELAPSED_SECS=$((ELAPSED_SECONDS % 60))
    echo "⏰ Runtime: ${ELAPSED_MINUTES}m ${ELAPSED_SECS}s"
    echo "📅 $(date '+%H:%M:%S')"
    echo ""
    
    # Check current startup phase
    STARTUP_SCRIPT=$(pgrep -f "run_stable_diffusion" | wc -l)
    VENV_CREATING=$(pgrep -f "python.*venv" | wc -l)
    TORCH_INSTALLING=$(pgrep -f "pip install.*torch" | wc -l)
    REQUIREMENTS_INSTALLING=$(pgrep -f "pip install.*requirements" | wc -l)
    XFORMERS_COMPILING=$(pgrep -f "nvcc.*flash" | wc -l)
    WEBUI_LAUNCHING=$(pgrep -f "python.*launch.py" | wc -l)
    
    # Determine current phase
    CURRENT_PHASE=""
    PHASE_PROGRESS=0
    TOTAL_PHASES=6
    
    if [ $STARTUP_SCRIPT -gt 0 ]; then
        if [ $VENV_CREATING -gt 0 ]; then
            CURRENT_PHASE="Creating virtual environment"
            PHASE_PROGRESS=1
        elif [ $TORCH_INSTALLING -gt 0 ]; then
            CURRENT_PHASE="Installing PyTorch & CUDA dependencies"
            PHASE_PROGRESS=2
        elif [ $REQUIREMENTS_INSTALLING -gt 0 ]; then
            CURRENT_PHASE="Installing WebUI requirements"
            PHASE_PROGRESS=3
        elif [ $XFORMERS_COMPILING -gt 0 ]; then
            CURRENT_PHASE="Compiling xformers (flash attention)"
            PHASE_PROGRESS=4
        elif [ $WEBUI_LAUNCHING -gt 0 ]; then
            CURRENT_PHASE="Loading models & starting WebUI"
            PHASE_PROGRESS=5
        else
            CURRENT_PHASE="Initializing startup"
            PHASE_PROGRESS=1
        fi
    elif [ $WEBUI_LAUNCHING -gt 0 ]; then
        CURRENT_PHASE="WebUI running, starting server"
        PHASE_PROGRESS=6
    else
        CURRENT_PHASE="Waiting for startup"
        PHASE_PROGRESS=0
    fi
    
    # Show startup progress
    echo "🚀 Startup Progress:"
    show_progress $TOTAL_PHASES $PHASE_PROGRESS "  Overall Progress"
    echo "  📋 Current Phase: $CURRENT_PHASE ${ANIMATION[$FRAME]}"
    echo ""
    
    # Show detailed process info
    echo "📊 Process Details:"
    if [ $STARTUP_SCRIPT -gt 0 ]; then
        echo "  ✅ Startup Script: Running"
    else
        echo "  ❌ Startup Script: Not found"
    fi
    
    if [ $TORCH_INSTALLING -gt 0 ]; then
        echo "  🔄 PyTorch Install: $TORCH_INSTALLING processes ${ANIMATION[$FRAME]}"
    elif [ $REQUIREMENTS_INSTALLING -gt 0 ]; then
        echo "  🔄 Requirements Install: $REQUIREMENTS_INSTALLING processes ${ANIMATION[$FRAME]}"
    elif [ $XFORMERS_COMPILING -gt 0 ]; then
        echo "  🔨 Xformers Compile: $XFORMERS_COMPILING processes ${ANIMATION[$FRAME]}"
    elif [ $WEBUI_LAUNCHING -gt 0 ]; then
        echo "  🎨 WebUI Launch: $WEBUI_LAUNCHING processes ${ANIMATION[$FRAME]}"
    else
        echo "  ⏸️  No active installs"
    fi
    echo ""
    
    # Check server status
    if ss -tlnp 2>/dev/null | grep -q ":7860" || curl -s --connect-timeout 1 http://localhost:7860 >/dev/null 2>&1; then
        echo "🌐 WebUI Server: ✅ READY on port 7860"
        echo ""
        echo "🎉 SUCCESS! Stable Diffusion WebUI is ready!"
        echo "   Access it at: http://localhost:7860"
        break
    else
        echo "🌐 WebUI Server: ⏳ Not ready yet"
    fi
    
    echo ""
    echo "═══════════════════════════════════════════════════════════════════"
    echo "💡 Tip: Xformers compilation for RTX 5090 can take 10-15 minutes"
    echo "⌨️  Press Ctrl+C to stop monitoring"
    echo ""
    
    # Update animation and elapsed time
    FRAME=$(( (FRAME + 1) % 10 ))
    sleep 1
    ELAPSED_SECONDS=$(( ELAPSED_SECONDS + 1 ))
done
