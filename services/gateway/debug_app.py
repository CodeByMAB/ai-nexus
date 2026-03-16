import sys
sys.stdout = sys.stderr  # Force all output to stderr for logging

from app import *

# Wrap the proxy function to log requests/responses
original_proxy = proxy

@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def debug_proxy(path: str, request: Request):
    body = await request.body()
    print(f"\n=== INCOMING REQUEST ===", file=sys.stderr)
    print(f"Path: /v1/{path}", file=sys.stderr)
    print(f"Body: {body[:500]}", file=sys.stderr)
    
    response = await original_proxy(path, request)
    
    if hasattr(response, 'body'):
        print(f"=== RESPONSE ===", file=sys.stderr)
        print(f"Body: {response.body[:500]}", file=sys.stderr)
    
    return response
