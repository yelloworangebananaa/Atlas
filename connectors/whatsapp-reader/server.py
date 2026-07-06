"""Read inbound WhatsApp messages via the bridge HTTP API."""
import os

import requests
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("whatsapp-reader")
BASE = os.environ.get("WHATSAPP_BRIDGE_URL", "http://localhost:3000")


@mcp.tool()
def read_whatsapp_messages() -> str:
    """Return unread inbound WhatsApp messages (marks them read)."""
    try:
        r = requests.get(f"{BASE}/receive", timeout=10)
        r.raise_for_status()
        msgs = r.json().get("messages") or []
        if not msgs:
            return "No new messages."
        return "\n".join(
            f"{m.get('pushName') or m.get('phoneJid') or m.get('jid')}: {m.get('text', '')}"
            for m in msgs
        )
    except Exception as e:
        return f"Error: {e}"


if __name__ == "__main__":
    mcp.run()
