# Cloudflare Tunnel Configuration for MCP Servers

## Working Solution: Expose Graphiti MCP

Since official MCP servers use stdio (not HTTP), the only working MCP server we can expose via Cloudflare Tunnel is **Graphiti MCP**, which runs on port 8001.

## Cloudflare Tunnel Configuration

### Current Tunnel Info
- **Tunnel Name:** `ai-brain`
- **Tunnel ID:** `78a21236-6b6e-4822-8f72-5376afa22ac3`
- **Management:** Remote (via Cloudflare Dashboard)

### Add MCP Ingress Rule

**Via Cloudflare Dashboard:**

1. Go to https://one.dash.cloudflare.com/
2. Navigate to **Zero Trust** → **Networks** → **Tunnels**
3. Find tunnel `ai-brain` and click **Configure**
4. Under **Public Hostname**, click **Add a public hostname**
5. Configure:
   - **Subdomain:** `mcp`
   - **Domain:** `YOURDOMAIN.COM`
   - **Type:** HTTP
   - **URL:** `localhost:8001`

6. Click **Save**

### Expected Ingress Configuration

After adding, your tunnel should have these routes:

```yaml
ingress:
  - hostname: ai.YOURDOMAIN.COM
    service: http://localhost:3000
  - hostname: n8n.YOURDOMAIN.COM
    service: http://localhost:5678
  - hostname: api.YOURDOMAIN.COM
    service: http://localhost:80
  - hostname: mcp.YOURDOMAIN.COM      # NEW
    service: http://localhost:8001              # NEW
  - service: http_status:404
```

### Restart Cloudflare Tunnel

After adding the rule via dashboard, restart the tunnel:

```bash
sudo systemctl restart cloudflared
```

Check status:
```bash
sudo systemctl status cloudflared
```

View logs:
```bash
sudo journalctl -u cloudflared -f
```

## Testing the MCP Endpoint

### Health Check (via Cloudflare)
```bash
curl https://mcp.YOURDOMAIN.COM/health
```

### Direct Local Test
```bash
curl http://localhost:8001/
```

### SSE Endpoint
The Graphiti MCP server exposes an SSE endpoint at:
```
https://mcp.YOURDOMAIN.COM/sse
```

## What This Gives You

### Graphiti Knowledge Graph MCP
- **Endpoint:** `https://mcp.YOURDOMAIN.COM/sse`
- **Purpose:** Knowledge graph-based persistent memory
- **Backend:** Neo4j database
- **Features:**
  - Add memory entities
  - Search memory nodes
  - Query memory facts
  - Entity relationships

### MCP Tools Available
When connected to Graphiti MCP, you get these tools:
- `add_memory` - Add new information to the knowledge graph
- `search_memory_nodes` - Search for entities
- `search_memory_facts` - Search for relationships
- `get_entity` - Retrieve entity details

## Connecting to Graphiti MCP

### From Claude Desktop (Example)
```json
{
  "mcpServers": {
    "graphiti-remote": {
      "url": "https://mcp.YOURDOMAIN.COM/sse",
      "transport": "sse"
    }
  }
}
```

### From Custom Client
Use SSE (Server-Sent Events) to connect:
```javascript
const eventSource = new EventSource('https://mcp.YOURDOMAIN.COM/sse');

eventSource.onmessage = (event) => {
  console.log('MCP Event:', event.data);
};
```

## Port Allocation Reference

Current port usage:
- **8001** - Graphiti MCP (exposed via mcp.YOURDOMAIN.COM)
- **8100** - nginx MCP gateway (reserved for future use)
- **8101-8200** - Reserved for additional MCP servers

## Troubleshooting

### DNS Issues
Verify DNS resolution:
```bash
nslookup mcp.YOURDOMAIN.COM
```

Should point to Cloudflare proxy.

### Tunnel Not Working
Check tunnel status:
```bash
sudo systemctl status cloudflared
sudo journalctl -u cloudflared --since "5 minutes ago"
```

Verify tunnel ingress:
```bash
# Look for the new hostname in logs
sudo journalctl -u cloudflared | grep "mcp.playground"
```

### Graphiti Not Responding
Check Graphiti container:
```bash
docker ps | grep graphiti
docker logs graphiti --tail 50
```

Verify Neo4j connection:
```bash
docker logs graphiti-neo4j --tail 20
```

Restart if needed:
```bash
cd /opt/ai/graphiti
docker-compose restart graphiti
```

### Port Already in Use
Check what's using port 8001:
```bash
sudo lsof -i :8001
```

or
```bash
sudo ss -tlnp | grep 8001
```

## Security Considerations

### No Authentication
Currently, the Graphiti MCP endpoint has **no authentication**. Anyone with the URL can access it.

**Recommendations:**
1. Add Cloudflare Access policy
2. Implement API key authentication
3. Use Cloudflare WAF rules
4. Monitor access logs

### Cloudflare Access (Optional)
To add authentication:
1. Go to **Zero Trust** → **Access** → **Applications**
2. **Add an application**
3. Select **Self-hosted**
4. Configure:
   - **Application name:** MCP Server
   - **Session duration:** 24 hours
   - **Domain:** `mcp.YOURDOMAIN.COM`
5. Add policy (e.g., email domain, IP range, etc.)

## Future Expansion

### Adding More HTTP-Compatible MCP Servers
If you find or build MCP servers that support HTTP/SSE:

1. Add to `/opt/ai/mcp-servers/docker-compose.yml`
2. Assign port from 8102-8200 range
3. Add nginx route in `nginx/nginx.conf`
4. Rebuild: `docker-compose up -d --build`
5. Add Cloudflare ingress rule
6. Restart tunnel

### Example: Adding Another Server
```yaml
# docker-compose.yml
services:
  new-mcp-server:
    image: some/mcp-server:latest
    ports:
      - "8102:8000"
    networks:
      - mcp-network
```

```nginx
# nginx.conf
location /newmcp/ {
    proxy_pass http://new-mcp-server:8000/;
    # ... proxy settings
}
```

Cloudflare:
- Subdomain: `mcp2.YOURDOMAIN.COM`
- Service: `http://localhost:8102`

## Summary

**What Works:**
- Graphiti MCP on port 8001 → `https://mcp.YOURDOMAIN.COM`
- Knowledge graph with Neo4j backend
- SSE transport for MCP protocol
- Secure access via Cloudflare Tunnel

**What To Add:**
1. Configure Cloudflare Tunnel ingress (you'll do this)
2. Optional: Add Cloudflare Access authentication
3. Optional: Add more HTTP-compatible MCP servers

**Next Steps:**
1. Add the ingress rule in Cloudflare Dashboard
2. Restart cloudflared service
3. Test the endpoint
4. Configure your MCP client to connect
