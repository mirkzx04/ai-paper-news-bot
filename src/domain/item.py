"""Domain entity `Item` — a normalized piece of content from any source.

No external dependencies and no I/O: this is the stable core every source
adapter maps *into* and every scorer/notifier reads *from*.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime

# arXiv ids look like 2406.01234 or 2406.01234v2 (post-2007 scheme).
_ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(?:v\d+)?")


@dataclass(frozen=True, slots=True)
class Item:
    """A paper, post, or other content item, normalized across sources."""

    source: str                         # "arxiv" | "s2" | "bluesky" | "hf"
    external_id: str                    # id stable *within* `source`
    title: str
    summary: str                        # abstract / post body
    url: str
    published: datetime                 # tz-aware UTC
    authors: tuple[str, ...] = ()       # display names
    author_ids: tuple[str, ...] = ()    # source-stable ids (e.g. S2 author id)
    categories: tuple[str, ...] = ()    # arxiv categories, hashtags, ...
    venue: str | None = None            # publication venue/conference if known, e.g. "NeurIPS 2025"
    extra: dict = field(default_factory=dict, compare=False)

    @property
    def arxiv_id(self) -> str | None:
        """Canonical arXiv id (version stripped) if this item maps to a paper.

        Lets a Bluesky post linking to an arXiv paper dedup against the paper
        pulled directly from the arXiv source.
        """
        match = _ARXIV_ID_RE.search(f"{self.external_id} {self.url}")
        return match.group(1) if match else None

    @property
    def canonical_key(self) -> str:
        """Cross-source dedup key: arXiv id when available, else source:id."""
        arxiv_id = self.arxiv_id
        return f"arxiv:{arxiv_id}" if arxiv_id else f"{self.source}:{self.external_id}"

    def text(self) -> str:
        """Title + summary, the input for keyword matching and embeddings."""
        return f"{self.title}\n\n{self.summary}".strip()


def normalize_name(name: str) -> str:
    """Lowercase, strip accents, collapse whitespace — for author-name matching."""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_name = decomposed.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_name).strip().lower()
