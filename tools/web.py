"""Web research tools (Spec §44, §71, §90).

Two rules shape this module:

* **Sources are always named.** Every result carries its URL, and the tool output tells the
  model to cite them. JARVIS must be able to distinguish what it read from what it knows, and
  must never claim to have researched something when no search ran.
* **Fetched content is untrusted.** A page can contain "ignore your instructions"; it is
  fenced as external data before the model ever sees it.

Search uses DuckDuckGo's HTML endpoint, which needs no API key and therefore no cost — in
line with the free-first requirement (Spec §7).
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

import httpx

from core.enums import Capability, RiskLevel
from core.errors import ToolError
from core.logging_setup import get_logger
from core.prompts import wrap_external
from tools.base import Tool, ToolContext, ToolResult

log = get_logger("tools.web")

USER_AGENT = "Mozilla/5.0 (compatible; JARVIS/0.1; personal assistant)"
MAX_PAGE_CHARS = 40_000
REQUEST_TIMEOUT = 25.0

# Domains whose documentation is authoritative for the topics this assistant deals with;
# the research agent prefers them (Spec §71).
TRUSTED_DOMAINS = (
    "docs.python.org", "developer.mozilla.org", "learn.microsoft.com", "docs.github.com",
    "github.com", "openrouter.ai", "ollama.com", "docs.ollama.com", "pypi.org",
    "bohemia.net", "community.bistudio.com", "server.nitrado.net", "wiki.nitrado.net",
    "discordpy.readthedocs.io", "discord.com", "home-assistant.io", "fastapi.tiangolo.com",
)


@dataclass(slots=True)
class SearchHit:
    title: str
    url: str
    snippet: str

    @property
    def domain(self) -> str:
        return urlparse(self.url).netloc.lower().removeprefix("www.")

    @property
    def trusted(self) -> bool:
        return any(self.domain == d or self.domain.endswith("." + d) for d in TRUSTED_DOMAINS)

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet,
                "domain": self.domain, "trusted": self.trusted}


_TAG = re.compile(r"<[^>]+>")
_SCRIPT_STYLE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)
_WHITESPACE = re.compile(r"\n\s*\n\s*\n+")
_RESULT_BLOCK = re.compile(
    r'<a[^>]+class="result__a"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>'
    r'.*?(?:<a[^>]+class="result__snippet"[^>]*>(?P<snippet>.*?)</a>)?',
    re.S | re.I,
)


def _clean(raw: str) -> str:
    return html.unescape(_TAG.sub("", raw or "")).strip()


def _decode_ddg_url(href: str) -> str:
    """DuckDuckGo wraps results in a redirect; unwrap it so the real source is cited."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg")
        if target:
            return target[0]
    return href


def html_to_text(raw: str) -> str:
    """Strip a page down to readable text. Deliberately simple — no parser dependency."""
    text = _SCRIPT_STYLE.sub(" ", raw)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", text, flags=re.I)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return _WHITESPACE.sub("\n\n", "\n".join(line for line in lines if line))


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "de,en;q=0.8"},
    )


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "Sucht im Internet und liefert Titel, URL und Kurzbeschreibung. "
        "Nutze anschließend fetch_page, um eine Quelle wirklich zu lesen."
    )
    risk_level = RiskLevel.SAFE_READ
    required_permission = Capability.WEB_SEARCH
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 2},
            "limit": {"type": "integer", "minimum": 1, "maximum": 15, "default": 6},
        },
        "required": ["query"],
    }

    def summarise(self, arguments: dict[str, Any]) -> str:
        return f"Im Internet suchen: '{arguments.get('query')}'"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        query = arguments["query"]
        limit = arguments.get("limit", 6)
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"

        try:
            async with await _client() as client:
                response = await client.post(
                    "https://html.duckduckgo.com/html/", data={"q": query}
                )
                if response.status_code >= 400:
                    response = await client.get(url)
        except httpx.HTTPError as exc:
            raise ToolError(
                f"search failed: {exc}",
                user_message="Die Internetsuche ist fehlgeschlagen. Besteht eine Verbindung?",
            ) from exc

        hits: list[SearchHit] = []
        for match in _RESULT_BLOCK.finditer(response.text):
            target = _decode_ddg_url(match.group("href"))
            if not target.startswith("http"):
                continue
            hits.append(SearchHit(
                title=_clean(match.group("title"))[:200],
                url=target,
                snippet=_clean(match.group("snippet") or "")[:400],
            ))
            if len(hits) >= limit:
                break

        if not hits:
            return ToolResult(
                ok=False,
                content="Die Suche lieferte keine auswertbaren Treffer.",
                display={"summary": "Keine Treffer", "query": query, "hits": []},
            )

        # Authoritative sources first (Spec §71).
        hits.sort(key=lambda h: (not h.trusted,))
        lines = [
            f"{i}. {h.title}\n   Quelle: {h.url}{' [offizielle Quelle]' if h.trusted else ''}\n"
            f"   {h.snippet}"
            for i, h in enumerate(hits, 1)
        ]
        return ToolResult(
            content=(
                f"Suchergebnisse für '{query}' ({len(hits)}):\n\n" + "\n\n".join(lines)
                + "\n\nDiese Liste besteht aus Titeln und Kurztexten. Öffne mit fetch_page eine "
                  "Quelle, bevor du inhaltliche Aussagen triffst, und nenne die URL."
            ),
            display={"summary": f"{len(hits)} Treffer für '{query}'", "query": query,
                     "hits": [h.to_dict() for h in hits]},
            citations=[{"title": h.title, "url": h.url} for h in hits],
        )


class FetchPageTool(Tool):
    name = "fetch_page"
    description = "Öffnet eine URL und gibt den lesbaren Textinhalt zurück."
    risk_level = RiskLevel.SAFE_READ
    required_permission = Capability.WEB_SEARCH
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "minLength": 8},
            "max_chars": {"type": "integer", "minimum": 500, "maximum": MAX_PAGE_CHARS,
                          "default": 15000},
        },
        "required": ["url"],
    }

    def summarise(self, arguments: dict[str, Any]) -> str:
        return f"Seite öffnen: {arguments.get('url')}"

    def scope(self, arguments: dict[str, Any]) -> str:
        return urlparse(str(arguments.get("url", ""))).netloc or "fetch_page"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        url = str(arguments["url"]).strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return ToolResult.failure("Nur http- und https-URLs werden unterstützt.")
        # Block requests aimed at the local machine or the private network: a page under
        # someone else's control must not be able to make JARVIS probe the user's LAN.
        host = (parsed.hostname or "").lower()
        if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".local"):
            return ToolResult.failure("Lokale Adressen werden aus Sicherheitsgründen nicht geöffnet.")
        if re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.)", host):
            return ToolResult.failure("Adressen im privaten Netz werden nicht geöffnet.")

        try:
            async with await _client() as client:
                response = await client.get(url)
        except httpx.HTTPError as exc:
            raise ToolError(
                f"fetch failed: {exc}",
                user_message=f"Die Seite konnte nicht geladen werden: {type(exc).__name__}",
            ) from exc

        if response.status_code >= 400:
            return ToolResult.failure(f"Die Seite antwortete mit HTTP {response.status_code}.")

        content_type = response.headers.get("content-type", "")
        if "html" in content_type:
            text = html_to_text(response.text)
        elif content_type.startswith("text/") or "json" in content_type or "xml" in content_type:
            text = response.text
        else:
            return ToolResult.failure(
                f"Der Inhaltstyp '{content_type or 'unbekannt'}' ist kein Text."
            )

        limit = arguments.get("max_chars", 15000)
        truncated = len(text) > limit
        text = text[:limit]

        return ToolResult(
            # The page content is fenced as untrusted data (Spec §90).
            content=wrap_external(text, source=str(response.url))
            + ("\n[… Seite wurde gekürzt …]" if truncated else "")
            + f"\n\nNenne diese Quelle, wenn du den Inhalt verwendest: {response.url}",
            display={"summary": f"{urlparse(str(response.url)).netloc} geladen "
                                f"({len(text)} Zeichen)",
                     "url": str(response.url), "chars": len(text), "truncated": truncated},
            citations=[{"title": urlparse(str(response.url)).netloc, "url": str(response.url)}],
        )


WEB_TOOLS = [WebSearchTool(), FetchPageTool()]
