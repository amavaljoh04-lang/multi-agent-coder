"""Web search integration via DuckDuckGo.

Provides a simple async interface to search the web and return
summarised results that can be injected into LLM prompts when
the model doesn't know something.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)

_DDG_URL = "https://html.duckduckgo.com/html/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean_html(text: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    return _WS_RE.sub(" ", _TAG_RE.sub("", text)).strip()


async def search_web(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """Search DuckDuckGo HTML and return a list of results.

    Each result is ``{"title": ..., "url": ..., "snippet": ...}``.
    Returns an empty list on any error (network, parsing, etc.).
    """
    results: list[dict[str, str]] = []
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=15, write=10, pool=10),
            follow_redirects=True,
        ) as client:
            resp = await client.post(
                _DDG_URL,
                data={"q": query, "b": ""},
                headers=_HEADERS,
            )
            resp.raise_for_status()
            html = resp.text

            # Parse result blocks from DDG HTML.
            for block in re.finditer(
                r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?'
                r'class="result__snippet"[^>]*>(.*?)</(?:td|div)',
                html,
                re.DOTALL,
            ):
                url = block.group(1).strip()
                title = _clean_html(block.group(2))
                snippet = _clean_html(block.group(3))

                # Skip DDG internal / ad links.
                if not url or "duckduckgo.com" in url:
                    continue

                # Unwrap DDG redirect URLs.
                uddg = re.search(r"uddg=([^&]+)", url)
                if uddg:
                    from urllib.parse import unquote
                    url = unquote(uddg.group(1))

                results.append({"title": title, "url": url, "snippet": snippet})
                if len(results) >= max_results:
                    break

    except Exception:
        log.warning("Web search failed for query=%r", query, exc_info=True)

    return results


def format_search_results(results: list[dict[str, str]]) -> str:
    """Format search results into a text block suitable for LLM context."""
    if not results:
        return ""
    lines = ["Web search results:"]
    for i, r in enumerate(results, 1):
        lines.append(f"\n[{i}] {r['title']}")
        lines.append(f"    URL: {r['url']}")
        lines.append(f"    {r['snippet']}")
    return "\n".join(lines)


async def search_and_format(query: str, max_results: int = 5) -> str:
    """Convenience: search + format in one call."""
    results = await search_web(query, max_results)
    return format_search_results(results)
