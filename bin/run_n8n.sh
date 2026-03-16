#!/bin/bash
cd ${HOME}

# Load environment configuration from env file
if [[ -f ${HOME}/.n8n/env ]]; then
    echo "Loading n8n configuration from ${HOME}/.n8n/env"
    set -a  # automatically export all variables
    source ${HOME}/.n8n/env
    set +a
else
    # Fallback defaults
    export N8N_USER_FOLDER=${HOME}/.n8n
    export N8N_HOST=0.0.0.0
    export N8N_PORT=5678
    export N8N_PROTOCOL=http
    export N8N_LOG_LEVEL=warn
    export N8N_SECURE_COOKIE=false
    export N8N_BASIC_AUTH_ACTIVE=false
    export N8N_METRICS=false
    export N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS=false
    export N8N_DISABLE_UI=false
    export N8N_DEFAULT_BINARY_DATA_MODE=filesystem
    # External (public) URLs stay https
    export N8N_EDITOR_BASE_URL=https://n8n.YOURDOMAIN.COM
    export WEBHOOK_URL=https://n8n.YOURDOMAIN.COM/
fi

# Ensure proper permissions
chmod 600 ${HOME}/.n8n/config 2>/dev/null || true

echo "🔧 Starting n8n with secure cookie disabled..."

# Try different n8n locations with fallback
if [[ -f "${HOME}/.nvm/versions/node/v22.18.0/bin/n8n" ]]; then
    echo "Using n8n from NVM..."
    exec ${HOME}/.nvm/versions/node/v22.18.0/bin/n8n start
elif command -v n8n >/dev/null 2>&1; then
    echo "Using system n8n..."
    exec n8n start
else
    echo "Installing n8n globally..."
    if command -v npm >/dev/null 2>&1; then
        npm install -g n8n@latest
        exec n8n start
    else
        echo "ERROR: npm not available, cannot install n8n"
        exit 1
    fi
fi
