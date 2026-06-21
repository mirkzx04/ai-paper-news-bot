"""Sync the bot's runtime state (data/) to a private GitHub gist.

The repo is public, so state (Telegram offset, seen-ids, profile overlay,
profile-vector cache, error/report logs) can't live on a branch. Instead the
whole `data/` directory is tar+gzip'd, base64-encoded, and stored as a single
file in a SECRET gist, read/written with a PAT (gist scope).

Usage (env GIST_ID + GIST_TOKEN required):
    python tools/state_store.py pull   # gist -> data/   (before a run)
    python tools/state_store.py push   # data/ -> gist   (after a run)
"""

from __future__ import annotations

import base64
import io
import os
import sys
import tarfile

import requests

_GIST_FILE = "state.b64"
_DATA_DIR = "data"
_API = "https://api.github.com/gists/{gist_id}"

# GitHub's gist API inlines a file's content in the JSON response only up to ~1 MB;
# beyond that it sets ``truncated`` and you must fetch ``raw_url`` (handled on pull).
# More importantly, a single gist file has a hard size ceiling, so the whole-state
# tar+base64 approach does NOT scale to many users (each adds a profile-vector and
# feedback-vector cache). We guard the PATCH: warn past 80% and FAIL past the limit
# rather than silently corrupting/truncating state.
#
# UPGRADE PATH when this limit is hit: stop tarring all of data/ into one blob —
# split per-user caches into separate gist files (or many gists), or move the large
# vector caches to external object storage (S3/R2) and keep only small metadata in
# the gist. Until then this guardrail makes the scaling wall loud and obvious.
_GIST_INLINE_LIMIT = 990_000   # ~0.99 MB of base64 chars (safely under ~1 MB)
_GIST_WARN_RATIO = 0.80


class StateTooLargeError(RuntimeError):
    """Raised by :func:`push` when the encoded state would exceed the gist limit."""


def _headers(token: str) -> dict:
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}


def pull(gist_id: str, token: str) -> None:
    resp = requests.get(_API.format(gist_id=gist_id), headers=_headers(token), timeout=30)
    resp.raise_for_status()
    file = resp.json().get("files", {}).get(_GIST_FILE)
    if not file:
        print("state_store: no state file in gist yet (first run)")
        return
    content = file.get("content", "")
    if file.get("truncated"):  # gist API inlines only < ~1 MB; fetch the rest
        content = requests.get(file["raw_url"], headers=_headers(token), timeout=30).text
    content = content.strip()
    if not content or content == "init":
        print("state_store: empty state (first run)")
        return
    blob = base64.b64decode(content)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        tar.extractall(".")  # archive holds the data/ prefix
    print(f"state_store: pulled state into {_DATA_DIR}/")


def push(gist_id: str, token: str) -> None:
    if not os.path.isdir(_DATA_DIR):
        print("state_store: no data/ to push")
        return
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(_DATA_DIR, arcname=_DATA_DIR)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    # Guardrail: never PATCH a blob that would overflow the gist's single-file
    # limit (which would corrupt/truncate everyone's state). Warn as we approach it.
    size = len(encoded)
    if size >= _GIST_INLINE_LIMIT:
        raise StateTooLargeError(
            f"state is {size} b64 chars, over the ~{_GIST_INLINE_LIMIT} gist limit. "
            "The single-gist whole-state store does not scale to this many users — "
            "split per-user caches into separate files/gists or move vector caches "
            "to external object storage (see tools/state_store.py for the upgrade path)."
        )
    if size >= _GIST_INLINE_LIMIT * _GIST_WARN_RATIO:
        print(f"state_store: WARNING state is {size} b64 chars, "
              f"{size * 100 // _GIST_INLINE_LIMIT}% of the gist limit — plan the upgrade.")
    resp = requests.patch(
        _API.format(gist_id=gist_id), headers=_headers(token),
        json={"files": {_GIST_FILE: {"content": encoded}}}, timeout=30,
    )
    resp.raise_for_status()
    print(f"state_store: pushed {len(encoded)} b64 chars to gist")


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in ("pull", "push"):
        sys.exit("usage: state_store.py {pull|push}")
    gist_id = os.environ.get("GIST_ID")
    token = os.environ.get("GIST_TOKEN")
    if not gist_id or not token:
        sys.exit("state_store: GIST_ID and GIST_TOKEN env vars are required")
    (pull if sys.argv[1] == "pull" else push)(gist_id, token)


if __name__ == "__main__":
    main()
