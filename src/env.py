"""Minimal .env loader (no dependency).

Loads KEY=VALUE lines into os.environ *without overriding* existing variables,
so real environment / CI secrets always win over a local .env file.
"""

from __future__ import annotations

import os


def load_env(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
