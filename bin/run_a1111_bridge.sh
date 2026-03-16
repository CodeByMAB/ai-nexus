#!/bin/bash
set -euo pipefail
cd ${HOME}/invokeai-a1111-bridge
source venv/bin/activate
export BRIDGE_HOST=0.0.0.0
export BRIDGE_PORT=7861
exec python bridge.py
