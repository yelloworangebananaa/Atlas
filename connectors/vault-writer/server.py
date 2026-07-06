"""Write content to a file path on disk

Agent-authored MCP server. Review this code, then enable it in the Connectors
panel (it is DISABLED by default).
"""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("vault-writer")

@mcp.tool()
def write_vault_file(path: str, content: str) -> str:
    """Write content to a file at the given path."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Successfully wrote to {path}"

if __name__ == "__main__":
    mcp.run()
