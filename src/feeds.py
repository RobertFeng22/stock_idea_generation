"""Resolve podcast feeds, list recent episodes, and locate transcripts.

First version policy: we only use transcripts that the podcast itself publishes
in its RSS feed (the Podcasting 2.0 ``<podcast:transcript>`` tag). Apple's own
auto-generated transcripts are not available outside the Apple app, so feeds
without a transcript tag are reported as "skipped (no transcript)" rather than
transcribed. Audio transcription (Whisper / cloud API) can be added later.

RSS is parsed with the standard library (xml.etree) so we have no native build
dependencies.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import requests

from .config import PodcastEntry

ITUNES_LOOKUP = "https://itunes.apple.com/lookup"
ITUNES_SEARCH = "https://itunes.apple.com/search"
USER_AGENT = "stock-idea-generation/1.0 (+podcast monitor)"

# Podcasting 2.0 namespace (transcript tag lives here).
PODCAST_NS = "https://podcastindex.org/namespace/1.0"

TRANSCRIPT_TYPES = {
    "application/json": "json",
    "text/vtt": "vtt",
    "application/x-subrip": "srt",
    "application/srt": "srt",
    "text/html": "html",
    "text/plain": "text",
}


@dataclass
class Episode:
    podcast: str
    title: str
    guid: str
    published: datetime | None
    link: str | None
    audio_url: str | None
    transcripts: list[dict] = field(default_factory=list)  # [{url, kind}]

    @property
    def published_str(self) -> str:
        return self.published.strftime("%Y-%m-%d") if self.published else "unknown date"


def _apple_id_from_url(url: str) -> str | None:
    m = re.search(r"id(\d+)", url)
    return m.group(1) if m else None


def resolve_feed_url(entry: PodcastEntry, session: requests.Session) -> str | None:
    """Return an RSS feed URL for a podcast entry, using iTunes if needed."""
    if entry.feed_url:
        return entry.feed_url

    if entry.apple_url:
        pid = _apple_id_from_url(entry.apple_url)
        if pid:
            try:
                r = session.get(ITUNES_LOOKUP, params={"id": pid}, timeout=30)
                results = r.json().get("results", [])
                if results and results[0].get("feedUrl"):
                    return results[0]["feedUrl"]
            except Exception as exc:  # noqa: BLE001
                print(f"  ! iTunes lookup failed for {entry.apple_url}: {exc}")

    if entry.name:
        try:
            r = session.get(
                ITUNES_SEARCH,
                params={"media": "podcast", "term": entry.name, "limit": 1},
                timeout=30,
            )
            results = r.json().get("results", [])
            if results and results[0].get("feedUrl"):
                return results[0]["feedUrl"]
        except Exception as exc:  # noqa: BLE001
            print(f"  ! iTunes search failed for {entry.name!r}: {exc}")

    return None


def _local(tag: str) -> str:
    """Strip the XML namespace from a tag name."""
    return tag.rsplit("}", 1)[-1]


def _parse_pubdate(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:  # noqa: BLE001
        return None


def _extract_transcripts(item: ET.Element) -> list[dict]:
    out = []
    for child in item:
        if _local(child.tag) != "transcript":
            continue
        url = child.get("url")
        if not url:
            continue
        ctype = (child.get("type") or "").lower().strip()
        out.append({"url": url, "kind": TRANSCRIPT_TYPES.get(ctype, "text")})
    return out


def _item_to_episode(item: ET.Element, show_title: str) -> Episode:
    title = guid = link = pubdate = None
    audio_url = None
    for child in item:
        tag = _local(child.tag)
        if tag == "title" and title is None:
            title = (child.text or "").strip()
        elif tag == "guid" and guid is None:
            guid = (child.text or "").strip()
        elif tag == "link" and link is None:
            link = (child.text or "").strip()
        elif tag == "pubDate":
            pubdate = child.text
        elif tag == "enclosure" and audio_url is None:
            audio_url = child.get("url")

    return Episode(
        podcast=show_title,
        title=title or "(untitled episode)",
        guid=guid or link or title or "",
        published=_parse_pubdate(pubdate),
        link=link,
        audio_url=audio_url,
        transcripts=_extract_transcripts(item),
    )


def list_recent_episodes(
    entry: PodcastEntry, since: datetime, session: requests.Session
) -> tuple[str | None, list[Episode]]:
    """Return (resolved_feed_url, episodes published on/after `since`)."""
    feed_url = resolve_feed_url(entry, session)
    if not feed_url:
        print(f"  ! Could not resolve a feed for {entry.label()!r}")
        return None, []

    try:
        r = session.get(feed_url, timeout=60, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as exc:  # noqa: BLE001
        print(f"  ! Failed to fetch/parse feed {feed_url}: {exc}")
        return feed_url, []

    channel = root.find("channel")
    if channel is None:
        # Some feeds may be Atom; not supported in v1.
        print(f"  ! Feed has no <channel> (Atom not supported yet): {feed_url}")
        return feed_url, []

    show_el = channel.find("title")
    show_title = (show_el.text.strip() if show_el is not None and show_el.text else entry.label())

    episodes: list[Episode] = []
    for item in channel.findall("item"):
        ep = _item_to_episode(item, show_title)
        # Only process episodes with a known publish date inside the lookback
        # window. Undated episodes are skipped so we never accidentally analyze
        # a feed's back catalogue (which would waste tokens).
        if ep.published is None or ep.published < since:
            continue
        episodes.append(ep)
    return feed_url, episodes
