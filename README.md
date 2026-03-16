# AI Nexus

> Self-hosted AI inference platform — OpenAI-compatible API, multi-model vLLM inference, payment processing, knowledge graph, agent memory, and a real-time operations dashboard.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Secret Scan](https://github.com/ai-nexus/ai-nexus/actions/workflows/secret-scan.yml/badge.svg)](https://github.com/ai-nexus/ai-nexus/actions/workflows/secret-scan.yml)

---

## Architecture

```
 ┌─────────────────────────────────────────────────────────┐
 │                    Cloudflare Tunnel                     │
 └──────┬──────────────────────┬──────────────────────┬────┘
        │                      │                      │
   api.domain.com        dash.domain.com        domain.com
        │                      │                      │
        ▼                      ▼                      ▼
  ┌──────────┐         ┌──────────────┐        ┌──────────┐
  │  nginx   │         │  Dashboard   │        │ Open     │
  │ :80      │         │  (FastAPI)   │        │ WebUI    │
  └────┬─────┘         │  :8200       │        │ :3000    │
       │               └──────────────┘        └──────────┘
       ▼
  ┌──────────────────────────────────┐
  │  API Gateway (FastAPI) :5050     │
  │  • Bearer token auth + metering  │
  │  • OpenAI-compatible API         │
  │  • Tool calling, SSE streaming   │
  │  • SDXL image routing            │
  └────┬─────────────┬───────────────┘
       │             │
       ▼             ▼
  ┌─────────┐  ┌──────────────┐
  │  vLLM   │  │  InvokeAI    │
  │ :11434  │  │  SDXL :9090  │
  └─────────┘  └──────────────┘

  ┌──────────┐  ┌──────────┐  ┌──────────┐
  │ Graphiti │  │   Zep    │  │   MCP    │
  │ + Neo4j  │  │ +Postgres│  │ Servers  │
  │ :8001    │  │ :8010    │  │ :8100    │
  └──────────┘  └──────────┘  └──────────┘

  ┌──────────────────────────────────┐
  │  Payment Webhook (FastAPI) :8003 │
  │  • Stripe, Strike (Bitcoin/LN)   │
  │  • API key provisioning          │
  └──────────────────────────────────┘
```

## Features

| Component | Description |
|-----------|-------------|
| **vLLM Inference** | Multi-mode: extreme (24B), code (24B), fast (14B), fast+image |
| **API Gateway** | OpenAI-compatible `/v1/chat/completions`, tool calling, SSE streaming |
| **Dashboard** | Real-time metrics, service controls, API key management |
| **Payment** | Stripe + Strike (Bitcoin/Lightning), auto-provision API keys |
| **Knowledge Graph** | Graphiti + Neo4j for agent long-term memory |
| **Agent Memory** | Zep + PostgreSQL for session context |
| **Image Generation** | SDXL 1.0 via InvokeAI |
| **MCP Servers** | GitHub, Docker Hub, filesystem, memory MCP endpoints |

## Hardware Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| GPU | NVIDIA 16GB VRAM (fast mode) | NVIDIA 24GB+ VRAM (RTX 3090/4090/5090) |
| RAM | 32GB | 64GB |
| Storage | 200GB NVMe | 1TB NVMe (for models) |
| OS | Ubuntu 22.04 LTS | Ubuntu 24.04 LTS |
| CUDA | 12.0+ | 12.8 (NVIDIA Driver 580+) |

> **Note for RTX 5090 / Blackwell users:** Set `VLLM_ATTENTION_BACKEND=TRITON_ATTN` and `--enforce-eager`. FlashInfer JIT compilation fails on compute_120a with CUDA < 13.0.

## Quick Start

### Option 1: Automated ISO Install

Download the latest release ISO, flash to USB, and boot:

```bash
# Flash to USB
sudo dd if=ai-nexus-installer-1.0.0-amd64.iso of=/dev/sdX bs=4M status=progress

# Boot from USB → automated install runs → system ready in ~20 min
```

### Option 2: Install on Existing Ubuntu 24.04

```bash
git clone https://github.com/ai-nexus/ai-nexus.git
cd ai-nexus
sudo bash installer/install.sh
```

### Option 3: Manual Setup

```bash
# 1. Configure environment
cp .env.example .env
nano .env   # fill in your domain, API keys, etc.

# 2. Start Docker services
docker compose up -d

# 3. Set up Python services
bash services/gateway/setup.sh
bash services/dashboard/setup.sh
bash services/payment-webhook/setup.sh

# 4. Install systemd services
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-gateway ai-dashboard payment-webhook

# 5. Configure nginx
sudo cp nginx/*.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

## Configuration

Copy `.env.example` to `.env` and configure:

```bash
cp .env.example .env
```

Key settings:

| Variable | Description | Example |
|----------|-------------|---------|
| `API_DOMAIN` | Public API subdomain | `api.example.com` |
| `DASHBOARD_DOMAIN` | Dashboard subdomain | `dash.example.com` |
| `STRIPE_SECRET_KEY` | Stripe secret key | `sk_live_...` |
| `NEO4J_PASSWORD` | Neo4j password (32+ chars) | `$(openssl rand -base64 32)` |
| `GPU_MEMORY_UTILIZATION` | vLLM VRAM utilization | `0.95` |

## Model Downloads

Models are not included — download separately:

```bash
export HF_HOME=/opt/models/huggingface

# Fast mode (14B, ~28GB) — recommended starting point
huggingface-cli download mistralai/Ministral-3-14B-Instruct-2512

# Extreme mode (24B, ~48GB)
huggingface-cli download mistralai/Mistral-Small-3.2-24B-Instruct-2506

# Code mode (24B, ~48GB)
huggingface-cli download mistralai/Devstral-Small-2-24B-Instruct-2512
```

## API Usage

```bash
# Chat completion
curl https://api.YOURDOMAIN.COM/v1/chat/completions \
  -H "Authorization: Bearer pg_YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "fast", "messages": [{"role": "user", "content": "Hello"}]}'

# Image generation (requires fast+image or image mode)
curl https://api.YOURDOMAIN.COM/v1/images/generations \
  -H "Authorization: Bearer pg_YOUR_API_KEY" \
  -d '{"prompt": "a futuristic city", "model": "sdxl-1.0"}'
```

## AI Modes

Switch modes via dashboard or CLI:

```bash
ai mode extreme    # Mistral-Small-24B, 49K context, best quality
ai mode code       # Devstral-24B, 65K context, coding specialist
ai mode fast       # Ministral-14B, 98K context, fast responses
ai mode fast+image # Ministral-14B + SDXL, multimodal
```

## Building the ISO

```bash
# Requires: xorriso p7zip-full wget
sudo apt install xorriso p7zip-full wget

bash installer/build-iso.sh --version 1.0.0
# Output: installer/ai-nexus-installer-1.0.0-amd64.iso
```

## License

Apache License 2.0 — see [LICENSE](LICENSE).

Note: Bundled models (Mistral family) are licensed under Apache 2.0. SDXL is licensed under OpenRAIL++-M. Neo4j Community Edition is GPL-3.0.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

See [SECURITY.md](SECURITY.md) for reporting vulnerabilities and security architecture.
