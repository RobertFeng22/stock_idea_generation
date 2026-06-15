"""Cache episode transcripts on disk so we never pay to transcribe the same
audio twice (e.g. when reprocessing after a prompt/format change).

Transcripts are stored under data/transcripts/<hash>.txt and committed back by
the workflow alongside seen.json.
"""
from __future__ import annotations

import hashlib

from .config import DATA_DIR

TRANSCRIPT_DIR = DATA_DIR / "transcripts"


def _key(podcast: str, guid: str) -> str:
    return hashlib.sha1(f"{podcast}::{guid}".encode("utf-8")).hexdigest()


def get_cached(podcast: str, guid: str) -> str | None:
    path = TRANSCRIPT_DIR / f"{_key(podcast, guid)}.txt"
    if path.exists():
        text = path.read_text(encoding="utf-8")
        return text if text.strip() else None
    return None


def cache_transcript(podcast: str, guid: str, text: str) -> None:
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    (TRANSCRIPT_DIR / f"{_key(podcast, guid)}.txt").write_text(text, encoding="utf-8")
