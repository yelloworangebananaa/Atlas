"""Expose Jarvis itself as an MCP server (spec §6), so another MCP client — an editor,
another agent — can drive it. One `ask_jarvis` tool runs a full Jarvis turn against the
same brain (config, vault, connectors, failover router) in-process.

Run:  .venv\\Scripts\\python -m jarvis.mcp_server
Another agent adds it as a stdio connector, e.g.
    {"name": "jarvis", "transport": "stdio",
     "command": ["<venv python>", "-m", "jarvis.mcp_server"], "enabled": true}

Transport is stdio (no network socket): this is an UNAUTHENTICATED LOCAL control surface,
same trust boundary as the machine itself (§6/T5). Do not expose it over the network.
"""
from mcp.server.fastmcp import FastMCP

from jarvis import agent

mcp = FastMCP("jarvis")


@mcp.tool()
def ask_jarvis(message: str) -> str:
    """Send a message to Jarvis and get its reply (runs a full tool-using turn)."""
    return agent.handle(message)


if __name__ == "__main__":
    import os

    if os.environ.get("JARVIS_MCP_CONNECTORS", "1") != "0":  # =0 to skip (fast handshake checks)
        from jarvis import mcp_client

        try:
            mcp_client.connect_all()  # so ask_jarvis can use the user's connectors too
        except Exception:
            pass
    mcp.run()
