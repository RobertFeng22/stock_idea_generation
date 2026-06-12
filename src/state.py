"""Track which episodes we've already processed, to avoid duplicates.

Stored as data/seen.json and committed back by the GitHub Action so state
survives across the ephemeral runners.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .config import DATA_DIR

SEEN_PATH = DATA_DIR / "seen.json"


def _key(podcast: str, guid: str) -> str:
    return hashlib.sha1(f"{podcast}::{guid}".encode("utf-8")).hexdigest()


def load_seen() -> set[str]:
    if not SEEN_PATH.exists():
        return set()
    try:
        return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        return set()


def mark_seen(seen: set[str], podcast: str, guid: str) -> None:
    seen.add(_key(podcast, guid))


def is_seen(seen: set[str], podcast: str, guid: str) -> bool:
    return _key(podcast, guid) in seen


def save_seen(seen: set[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(sorted(seen), indent=2), encoding="utf-8")
