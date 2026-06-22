"""Atomic, durable text writes for the JSON/JSONL state stores.

State management best practice: never write a state file in place. A crash or
kill mid-write leaves a truncated/garbage file — and in the gist-synced
deployment that corruption is then tarred up and pushed to the gist, poisoning
every later run. Writing to a temp file in the SAME directory and then
``os.replace`` makes the swap atomic: a reader (or the next run) always sees
either the old complete file or the new complete file, never a partial one.

This is the single home for the pattern that was previously hand-rolled in
``error_log`` and ``privacy``; the durable stores (registry, profile overlay,
reports) now share it.
"""

from __future__ import annotations

import os
from pathlib import Path


def atomic_write_text(path: str | Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically and durably (flush + fsync + rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # The temp file MUST share the target's directory: os.replace is only atomic
    # within one filesystem, and a rename across filesystems is a copy.
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())  # bytes are on disk before the swap
    os.replace(tmp, path)      # atomic swap
    # ponytail: no directory fsync — the gist push is the real durability layer,
    # so power-loss durability of the rename entry itself isn't worth the dir-open.


def demo() -> None:
    """Self-check: the swap is all-or-nothing and a stale temp never wins."""
    import tempfile

    d = tempfile.mkdtemp()
    p = Path(d) / "sub" / "state.json"
    atomic_write_text(p, '{"v": 1}')
    assert p.read_text() == '{"v": 1}'
    atomic_write_text(p, '{"v": 2}')          # overwrite
    assert p.read_text() == '{"v": 2}'
    assert not (p.parent / f".{p.name}.tmp").exists()  # temp cleaned up by replace
    print("atomic_write demo ok")


if __name__ == "__main__":
    demo()
