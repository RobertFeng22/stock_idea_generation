"""Fetch and normalize transcripts published in podcast RSS feeds."""
from __future__ import annotations

import html
import json
import re

import requests

USER_AGENT = "stock-idea-generation/1.0 (+podcast monitor)"
MAX_BYTES = 8 * 1024 * 1024  # 8 MB safety cap on a transcript download


def fetch_transcript(transcripts: list[dict], session: requests.Session) -> str | None:
    """Try each transcript URL in order; return plain text or None.

    We prefer richer formats (json/vtt/srt) before falling back to html/text.
    """
    order = {"json": 0, "vtt": 1, "srt": 2, "text": 3, "html": 4}
    for t in sorted(transcripts, key=lambda x: order.get(x["kind"], 5)):
        try:
            r = session.get(t["url"], timeout=60, headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            if len(r.content) > MAX_BYTES:
                continue
            text = _to_text(r.text, t["kind"])
            if text and len(text.strip()) > 200:  # ignore near-empty stubs
                return text.strip()
        except Exception as exc:  # noqa: BLE001
            print(f"    ! transcript fetch failed ({t['url']}): {exc}")
    return None


def _to_text(body: str, kind: str) -> str:
    if kind == "json":
        return _from_json(body)
    if kind in ("vtt", "srt"):
        return _from_caption(body)
    if kind == "html":
        return _from_html(body)
    return body


def _from_json(body: str) -> str:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return ""
    # Podcasting 2.0 transcript JSON: {"version": "...", "segments": [{"body": "..."}]}
    segments = data.get("segments") if isinstance(data, dict) else None
    if isinstance(segments, list):
        parts = [s.get("body", "") for s in segments if isinstance(s, dict)]
        return " ".join(p.strip() for p in parts if p).strip()
    return ""


def _from_caption(body: str) -> str:
    lines = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith("WEBVTT"):
            continue
        if "-->" in line:  # timestamp line
            continue
        if line.isdigit():  # SRT cue index
            continue
        # Strip inline caption tags like <c> or <00:00:01.000>
        line = re.sub(r"<[^>]+>", "", line)
        lines.append(line)
    return _dedupe(" ".join(lines))


def _from_html(body: str) -> str:
    body = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", body)
    body = re.sub(r"(?i)<br\s*/?>", "\n", body)
    body = re.sub(r"(?i)</p>", "\n", body)
    text = re.sub(r"<[^>]+>", " ", body)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _dedupe(text: str) -> str:
    """Caption files often repeat rolling lines; collapse immediate repeats."""
    words = text.split()
    out = []
    for w in words:
        if out and out[-1] == w:
            continue
        out.append(w)
    return " ".join(out)
