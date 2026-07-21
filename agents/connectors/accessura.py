"""
Accessura connector — fetch Politics and Sports intelligence Packs from the
Accessura data marketplace for use as Polymarket agent signal inputs.

Usage:
    from agents.connectors.accessura import Accessura
    accessura = Accessura()
    articles = accessura.get_articles_for_cli_keywords("policy,election,injury")
"""

import os
from typing import Optional

from agents.utils.objects import Article, Source


class Accessura:
    """Connector for the Accessura Politics and Sports data marketplace.

    Fetches structured packs (text reports, match statistics, photos, video/audio)
    published by automated crawlers and institutional sellers. Each pack-signal
    is mapped to a Polymarket-compatible Article object so the existing agent
    pipeline (RAG → LLM → trade) consumes it without changes.
    """

    def __init__(self) -> None:
        self.base_url = os.getenv(
            "ACCESSURA_BASE_URL", "https://testnet.accessura.io"
        )
        self.api_base = f"{self.base_url}/api/v1"
        # Bearer token from agent registration + challenge login.
        # Agents register once with register_identity + challenge → token,
        # then set ACCESSURA_TOKEN. If unset, the public (unauthenticated)
        # pack listing is used (no purchase/delivery).
        self._token: Optional[str] = os.getenv("ACCESSURA_TOKEN")

    # ── public interface (matches existing connector pattern) ──────────

    def get_articles_for_cli_keywords(self, keywords: str) -> "list[Article]":
        """Return Article objects for packs matching comma-separated keywords.

        Maps each keyword to a pack title/summary search, then flattens all
        signal-level content into Article objects the RAG pipeline can ingest.
        """
        query_words = [w.strip() for w in keywords.split(",") if w.strip()]
        all_articles: list[Article] = []
        seen = set()

        for word in query_words:
            packs = self._search_packs(word)
            for pack in packs:
                for signal in pack.get("signals", []):
                    article = self._signal_to_article(pack, signal)
                    key = article.title or article.url
                    if key and key not in seen:
                        seen.add(key)
                        all_articles.append(article)

        return all_articles

    def get_top_articles_for_market(self, market_object: dict) -> "list[Article]":
        """Search Accessura for packs relevant to a Polymarket market description."""
        query = market_object.get("description") or market_object.get("title", "")
        return self.get_articles_for_cli_keywords(query)

    # ── internal helpers ───────────────────────────────────────────────

    def _search_packs(self, keyword: str, limit: int = 20) -> list[dict]:
        """Search public pack listing by keyword in title/summary."""
        import urllib.request
        import json

        url = f"{self.api_base}/packs?q={urllib.request.quote(keyword)}&limit={limit}"
        headers = {"User-Agent": "Accessura-Polymarket-Connector/0.1"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                return data.get("packs", [])
        except Exception:
            return []

    def _signal_to_article(self, pack: dict, signal: dict) -> Article:
        """Map one Accessura pack-signal to a Polymarket Article."""
        title = signal.get("label") or pack.get("title", "")
        summary = signal.get("summary") or pack.get("summary", "")
        body_parts = [summary]
        payload = signal.get("payload", {})
        if payload:
            body_parts.append(str(payload))

        return Article(
            source=Source(
                id="accessura",
                name=pack.get("sourceDeclaration") or "Accessura",
            ),
            author=pack.get("sellerId"),
            title=title,
            description=summary[:500] if summary else None,
            url=f"{self.base_url}/packs/{pack.get('id', '')}",
            urlToImage=None,
            publishedAt=signal.get("observedAt") or pack.get("publishedAt"),
            content="\n".join(body_parts),
        )
