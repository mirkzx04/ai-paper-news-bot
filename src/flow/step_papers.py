"""Papers step of the /creare_profile onboarding flow.

Turns a user's list of paper titles (one per line) into saved arXiv seed ids.
The title -> arXiv resolution is injected via `resolve` so the step stays pure
and unit-testable without any network access.
"""

from __future__ import annotations

from typing import Callable, Optional, Tuple

# A resolver maps a free-text title to (arxiv_id, matched_title); both None on miss.
Resolver = Callable[[str], Tuple[Optional[str], Optional[str]]]


def handle_papers(text: str, profile_store, resolve: Resolver) -> str:
    """Resolve each title line, persist the found seed ids, and build a reply.

    `text`          user message, one paper title per line (blank lines ignored).
    `profile_store` a ProfileStore exposing add_seed_ids(list[str]) -> list[str].
    `resolve`       resolve(title) -> (arxiv_id | None, matched_title | None).
    """
    # Keep only non-empty, stripped title lines.
    titles = [line.strip() for line in text.splitlines() if line.strip()]

    # No usable input: return a usage hint.
    if not titles:
        return (
            "Send me the paper titles, one per line, "
            "and I'll look them up on arXiv."
        )

    found: list[Tuple[str, str]] = []  # (arxiv_id, matched_title)
    not_found: list[str] = []          # original titles we could not resolve

    for title in titles:
        arxiv_id, matched = resolve(title)
        if arxiv_id is not None:
            # Fall back to the user's title if the resolver gave no matched title.
            found.append((arxiv_id, matched if matched is not None else title))
        else:
            not_found.append(title)

    # Persist every found id in a single store mutation.
    if found:
        profile_store.add_seed_ids([arxiv_id for arxiv_id, _ in found])

    # Build the reply text.
    lines: list[str] = []

    if found:
        lines.append("Found these papers on arXiv:")
        for arxiv_id, matched in found:
            lines.append(f"✅ {matched} (arXiv:{arxiv_id})")

    # The not-found line MUST be exactly this English sentence (per spec).
    for title in not_found:
        lines.append(f"At the moment {title} is not available.")

    return "\n".join(lines)
