#!/usr/bin/env python3
"""Runnable MCP server exposing the token-saver tools (deterministic)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def main_entry():
    from mcp_token_saver import register_token_saver_tools
    try:
        from mcp.server.fastmcp import FastMCP
    except Exception as e:
        print("mcp not installed:", e); sys.exit(1)
    mcp = FastMCP("mcp-token-saver")
    register_token_saver_tools(mcp)
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--http", action="store_true")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=9400)
    a = p.parse_args()
    if a.http:
        import uvicorn
        print(f"[mcp-token-saver] Streamable HTTP on {a.host}:{a.port}", flush=True)
        uvicorn.run(mcp.streamable_http_app(), host=a.host, port=a.port, log_level="warning")
    else:
        mcp.run()

if __name__ == "__main__":
    main_entry()
