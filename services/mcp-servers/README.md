# MCP Servers Infrastructure

This directory contains a unified MCP (Model Context Protocol) server setup, exposing multiple MCP servers through a single nginx gateway.

## Architecture

```
Cloudflare Tunnel (mcp.YOURDOMAIN.COM)
    ↓
Port 8100 (nginx-mcp-gateway)
    ↓
    ├─ /git/*         → git-mcp:8000
    ├─ /brave/*       → brave-search-mcp:8000
    ├─ /filesystem/*  → filesystem-mcp:8000
    ├─ /memory/*      → memory-mcp:8000
    └─ /graphiti/*    → host.docker.internal:8001 (existing)
```

## Port Allocation

Reserved range: **8100-8200** (100 ports for MCP servers)

Current assignments:
- **8100** - Nginx MCP Gateway (public-facing via Cloudflare)
- **8101** - Git MCP Server (internal)
- **8102** - Brave Search MCP Server (internal)
- **8103** - Filesystem MCP Server (internal)
- **8104** - Memory MCP Server (internal)
- **8105-8200** - Reserved for future MCP servers (95 slots available)

## MCP Servers

### 1. Git MCP Server
- **Endpoint:** `https://mcp.YOURDOMAIN.COM/git/`
- **Purpose:** Read, search, and manipulate Git repositories
- **Access:** Read-only access to `/opt/ai` and `/home`

### 2. Brave Search MCP Server
- **Endpoint:** `https://mcp.YOURDOMAIN.COM/brave/`
- **Purpose:** Web search capabilities via Brave Search API
- **Requires:** `BRAVE_API_KEY` environment variable

### 3. Filesystem MCP Server
- **Endpoint:** `https://mcp.YOURDOMAIN.COM/filesystem/`
- **Purpose:** Secure file operations with access controls
- **Access:** Read/write to `/opt/ai` and `${HOME}`

### 4. Memory MCP Server
- **Endpoint:** `https://mcp.YOURDOMAIN.COM/memory/`
- **Purpose:** Knowledge graph-based persistent memory
- **Requires:** `OPENAI_API_KEY` environment variable

### 5. Graphiti MCP Server (existing)
- **Endpoint:** `https://mcp.YOURDOMAIN.COM/graphiti/`
- **Purpose:** Advanced knowledge graph with Neo4j backend
- **Note:** Runs separately at `/opt/ai/graphiti/`

## Setup Instructions

### 1. Environment Configuration

Copy the example environment file:
```bash
cd /opt/ai/mcp-servers
cp .env.example .env
```

Edit `.env` and add your API keys:
```bash
BRAVE_API_KEY=your_actual_brave_api_key
OPENAI_API_KEY=your_actual_openai_api_key
```

### 2. Build and Start Services

```bash
cd /opt/ai/mcp-servers
docker-compose up -d --build
```

### 3. Verify Services

Check all containers are running:
```bash
docker-compose ps
```

Test the gateway:
```bash
curl http://localhost:8100/health
```

View logs:
```bash
docker-compose logs -f
```

### 4. Configure Cloudflare Tunnel

Add this ingress rule to your Cloudflare Tunnel configuration (via web dashboard):

```yaml
- hostname: mcp.YOURDOMAIN.COM
  service: http://localhost:8100
```

Place this rule **before** the catch-all 404 rule.

After adding, restart the tunnel:
```bash
sudo systemctl restart cloudflared
```

### 5. Test Public Access

Once Cloudflare Tunnel is configured:
```bash
curl https://mcp.YOURDOMAIN.COM/health
curl https://mcp.YOURDOMAIN.COM/
```

## Management

### Start Services
```bash
docker-compose up -d
```

### Stop Services
```bash
docker-compose down
```

### View Logs
```bash
# All services
docker-compose logs -f

# Specific service
docker-compose logs -f git-mcp
docker-compose logs -f nginx-mcp-gateway
```

### Restart a Service
```bash
docker-compose restart git-mcp
```

### Rebuild After Changes
```bash
docker-compose up -d --build
```

## Adding New MCP Servers

To add a new MCP server:

1. Create a new directory: `mkdir new-mcp-server`
2. Create Dockerfile in that directory
3. Add service to `docker-compose.yml`
4. Add route in `nginx/nginx.conf`
5. Rebuild: `docker-compose up -d --build`

Available ports: 8105-8200

## Troubleshooting

### Check container status
```bash
docker ps -a | grep mcp
```

### Check network connectivity
```bash
docker exec mcp-gateway ping git-mcp
```

### Test individual MCP server
```bash
docker exec -it git-mcp curl http://localhost:8000/health
```

### Check nginx configuration
```bash
docker exec mcp-gateway nginx -t
```

### View nginx access logs
```bash
docker exec mcp-gateway tail -f /var/log/nginx/access.log
```

## API Keys

### Brave Search API
- Get your key: https://brave.com/search/api/
- Free tier: 2,000 queries/month

### OpenAI API
- Get your key: https://platform.openai.com/api-keys
- Required for Memory MCP server

## Security Notes

- Only nginx gateway (port 8100) is exposed to the host
- Internal MCP servers communicate via Docker network
- Filesystem access is limited to specific directories
- Git repositories are read-only

## Related Services

- **Open WebUI:** `https://ai.YOURDOMAIN.COM` (port 3000)
- **n8n:** `https://n8n.YOURDOMAIN.COM` (port 5678)
- **API:** `https://api.YOURDOMAIN.COM` (port 80)
- **Graphiti (standalone):** `http://localhost:8001` (not via gateway)

## Support

For issues or questions:
- Check logs: `docker-compose logs -f`
- Verify environment: `docker-compose config`
- Test connectivity: Use curl commands above
