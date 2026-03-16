# MCP Server Architecture - Important Information

## Discovery: MCP Servers Use stdio, Not HTTP

After setting up the infrastructure, I discovered that **official MCP servers communicate via stdin/stdout (stdio) using JSON-RPC**, not via HTTP/SSE endpoints.

This means:
- MCP servers **cannot** be exposed via nginx reverse proxy
- MCP servers **cannot** be accessed via Cloudflare Tunnel like traditional web services
- The current Docker/nginx setup will **not work** as originally planned

## What MCP Servers Actually Are

MCP (Model Context Protocol) servers are designed to be:
1. **Process-based** - They run as standalone processes
2. **stdio-based** - Communication happens via standard input/output
3. **Direct connection** - MCP clients (like Claude Desktop) connect directly to the server process
4. **Not HTTP services** - They don't listen on TCP ports or serve HTTP requests

## Current Setup Status

### What Works ✓
- All Docker containers build successfully
- Directory structure is in place: `/opt/ai/mcp-servers/`
- Nginx gateway is running on port 8100
- Graphiti MCP server (different implementation) works on port 8001

### What Doesn't Work ✗
- Official MCP servers (filesystem, memory, github) cannot be accessed via HTTP
- nginx cannot route to stdio-based services
- Cloudflare Tunnel cannot expose stdio services

## Solutions & Alternatives

### Option 1: Use Graphiti-style MCP Servers (Recommended)
The Graphiti MCP server at `/opt/ai/graphiti/` actually **does** expose an HTTP/SSE endpoint. This is a custom implementation.

**Approach:**
- Look for MCP servers that explicitly support HTTP/SSE transport
- Or build custom wrappers around stdio MCP servers to expose them via HTTP
- The `zep-ai/knowledge-graph-mcp` image used by Graphiti supports `--transport sse`

### Option 2: Run MCP Servers Locally on Client
The standard approach for MCP servers:
```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/files"]
    },
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "your_token"
      }
    }
  }
}
```

This runs the MCP server as a local process that Claude Desktop connects to directly.

### Option 3: Build HTTP Wrapper for stdio MCP Servers
Create a Node.js service that:
1. Spawns MCP server processes
2. Communicates with them via stdin/stdout
3. Exposes an HTTP/SSE API for external access
4. Routes requests between HTTP clients and stdio MCP servers

**This would require custom development.**

### Option 4: Use MCP Servers with SSE Support
Search for MCP servers that explicitly support `--transport sse` or HTTP modes:
- Some community MCP servers may support this
- Check each server's documentation
- Graphiti's implementation is an example of this working

## What We Can Do Right Now

### 1. Keep Graphiti MCP Working
Graphiti MCP at `http://localhost:8001` already works and can be exposed via Cloudflare:

```yaml
# Add to Cloudflare Tunnel ingress
- hostname: mcp.YOURDOMAIN.COM
  service: http://localhost:8001
```

This gives you **one working MCP server** (knowledge graph) accessible remotely.

### 2. Use Official MCP Servers Locally
Install MCP servers on the client machine (your laptop/desktop) where Claude Desktop runs:
- They'll work perfectly in that context
- No Docker or HTTP needed
- This is the intended use case

### 3. Research SSE-Compatible MCP Servers
Look for:
- Community MCP servers with HTTP support
- Forks of official servers with SSE transport
- Alternative implementations like Graphiti

## Current Infrastructure Value

Even though the HTTP/nginx approach doesn't work for official MCP servers, the infrastructure created is still valuable:

### Nginx Gateway (Port 8100)
- Can route to HTTP-based services
- Works for Graphiti MCP
- Can be used for future HTTP-compatible MCP servers
- Good foundation if we build HTTP wrappers

### Docker Setup
- Clean, reproducible environment
- Easy to add new services
- Port range 8100-8200 reserved for future use
- Good base for custom MCP implementations

### Cloudflare Tunnel
- Can expose Graphiti MCP
- Can expose any future HTTP-based MCP services
- Secure, authenticated access

## Recommended Next Steps

1. **Expose Graphiti MCP via Cloudflare Tunnel**
   - Add tunnel ingress for `mcp.YOURDOMAIN.COM -> http://localhost:8001`
   - This gives you one working remote MCP server

2. **Use Official MCP Servers Locally**
   - Install them on client machines via npx
   - Configure in Claude Desktop settings
   - This is the standard, supported approach

3. **Research HTTP-Compatible Alternatives**
   - Look for community MCP servers with SSE support
   - Consider building HTTP wrappers if needed
   - Explore alternative knowledge graph implementations

4. **Keep Infrastructure for Future Use**
   - Don't delete the Docker setup
   - It's ready for HTTP-compatible MCP servers
   - Can be extended with custom implementations

## Technical Details

### Why stdio?
- Simplicity: No need for HTTP server infrastructure
- Security: Process isolation, no network exposure
- Performance: Direct IPC, no HTTP overhead
- Design: MCP servers are meant to be local tools

### Why Graphiti Works?
The Graphiti MCP server uses a different base:
- Built with FastMCP framework
- Explicitly supports `--transport sse`
- Runs uvicorn HTTP server
- Different from official @modelcontextprotocol servers

### HTTP Wrapper Complexity
Building an HTTP wrapper requires:
- Process management (spawn/kill MCP servers)
- stdin/stdout streaming
- JSON-RPC protocol handling
- SSE event stream generation
- Error handling and reconnection logic
- Security/authentication layer

This is non-trivial but doable if needed.

## Conclusion

The MCP ecosystem is designed around local, process-based servers. Remote HTTP access is not the primary use case.

**For remote access:**
- Use Graphiti MCP (already works!)
- Or build custom HTTP wrappers
- Or find community servers with HTTP support

**For local use:**
- Official MCP servers work great
- Standard installation via npx
- Configure in Claude Desktop

The infrastructure we built is solid and ready for HTTP-compatible MCP servers when we find or build them.
