"""A web search MCP tool server that searches the web and returns results.

Agent-authored MCP server. Review this code, then enable it in the Connectors
panel (it is DISABLED by default).
"""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("web-search")

import urllib.request
import urllib.parse
import json
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("web-search")


def _search_duckduckgo(query: str, num_results: int = 5) -> list[dict]:
    """Search DuckDuckGo HTML and parse results."""
    url = "https://html.duckduckgo.com/html/"
    data = urllib.parse.urlencode({"q": query}).encode()
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        html = resp.read().decode("utf-8", errors="ignore")

    results = []
    # Parse DuckDuckGo HTML results
    import re
    links = re.findall(r'<a rel="nofollow" class="result__a" href="([^"]+)">(.*?)</a>', html, re.DOTALL)
    snippets = re.findall(r'<a class="result__snippet"[^>]*>(.*?)</a>', html, re.DOTALL)

    for i, (link, title) in enumerate(links[:num_results]):
        # Clean HTML from title
        clean_title = re.sub(r'<[^>]+>', '', title).strip()
        # Decode the redirect URL
        if "uddg=" in link:
            actual_url = urllib.parse.unquote(
                re.search(r'uddg=([^&]+)', link).group(1)
            )
        else:
            actual_url = link
        snippet = ""
        if i < len(snippets):
            snippet = re.sub(r'<[^>]+>', '', snippets[i]).strip()
        results.append({
            "title": clean_title,
            "url": actual_url,
            "snippet": snippet
        })
    return results


@mcp.tool()
def web_search(query: str, num_results: int = 5) -> str:
    """Search the web and return results. Returns JSON with title, url, and snippet for each result.

    Args:
        query: The search query string.
        num_results: Number of results to return (default 5, max 10).
    """
    num_results = min(num_results, 10)
    try:
        results = _search_duckduckgo(query, num_results)
        if not results:
            return json.dumps({"query": query, "results": [], "message": "No results found."})
        return json.dumps({"query": query, "results": results}, indent=2)
    except Exception as e:
        return json.dumps({"query": query, "error": str(e)})


@mcp.tool()
def fetch_url(url: str) -> str:
    """Fetch the content of a web page URL and return the text.

    Args:
        url: The URL to fetch.
    """
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        # Strip HTML tags for readable text
        import re
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        # Truncate to avoid huge responses
        if len(text) > 5000:
            text = text[:5000] + "... [truncated]"
        return text
    except Exception as e:
        return f"Error fetching URL: {str(e)}"


if __name__ == "__main__":
    mcp.run()
