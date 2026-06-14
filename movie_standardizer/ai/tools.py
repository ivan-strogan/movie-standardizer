"""Tool implementations available to the Ollama agent.

Currently provides:
  web_search(query) — DuckDuckGo HTML search, no API key required.
"""

from __future__ import annotations

import httpx
from bs4 import BeautifulSoup


# ── Tool schemas (Ollama tool-use format) ─────────────────────────────────────

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web using DuckDuckGo and return the top results. "
                "Use this to look up a movie title, confirm a release year, "
                "or resolve an ambiguous torrent name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query, e.g. 'Blind Date 1987 film'",
                    }
                },
                "required": ["query"],
            },
        },
    },
]


# ── Tool dispatch ─────────────────────────────────────────────────────────────

def dispatch(name: str, args: dict) -> dict:
    if name == "web_search":
        return web_search(args.get("query", ""))
    return {"error": f"Unknown tool: {name}"}


# ── Implementations ───────────────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def web_search(query: str, max_results: int = 5) -> dict:
    """Search DuckDuckGo and return top result snippets."""
    if not query.strip():
        return {"error": "Empty query"}

    try:
        url = "https://html.duckduckgo.com/html/"
        resp = httpx.post(
            url,
            data={"q": query, "kl": "us-en"},
            headers=_HEADERS,
            timeout=10,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except Exception as exc:
        return {"error": f"Search request failed: {exc}"}

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    for result in soup.select(".result")[:max_results]:
        title_el = result.select_one(".result__title")
        snippet_el = result.select_one(".result__snippet")
        url_el = result.select_one(".result__url")

        title   = title_el.get_text(strip=True) if title_el else ""
        snippet = snippet_el.get_text(strip=True) if snippet_el else ""
        link    = url_el.get_text(strip=True) if url_el else ""

        if title or snippet:
            results.append({"title": title, "snippet": snippet, "url": link})

    if not results:
        return {"error": "No results found", "query": query}

    return {"query": query, "results": results}
