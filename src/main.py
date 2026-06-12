"""Weekly pipeline: monitor podcasts -> get transcripts -> analyze -> email.

Run with:  python -m src.main
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from anthropic import Anthropic

from . import state
from .analyze import analyze_transcript
from .config import DATA_DIR, load_settings
from .emailer import EpisodeResult, build_html, send_email
from .feeds import list_recent_episodes
from .transcripts import fetch_transcript


def run() -> int:
    settings = load_settings()
    since = datetime.now(timezone.utc) - timedelta(days=settings.lookback_days)
    print(f"Looking for episodes published since {since.date()} "
          f"across {len(settings.podcasts)} podcast(s).")

    session = requests.Session()
    client = Anthropic(api_key=settings.anthropic_api_key)
    seen = state.load_seen()

    results: list[EpisodeResult] = []

    for entry in settings.podcasts:
        print(f"\n== {entry.label()} ==")
        feed_url, episodes = list_recent_episodes(entry, since, session)
        if feed_url and not entry.feed_url:
            print(f"  resolved feed: {feed_url}")
        if not episodes:
            print("  no new episodes in window.")
            continue

        for ep in episodes:
            if state.is_seen(seen, ep.podcast, ep.guid):
                print(f"  - already processed: {ep.title}")
                continue
            print(f"  + new episode: {ep.title} ({ep.published_str})")
            state.mark_seen(seen, ep.podcast, ep.guid)

            transcript = None
            if ep.transcripts:
                transcript = fetch_transcript(ep.transcripts, session)

            if not transcript:
                results.append(EpisodeResult(
                    podcast=ep.podcast, title=ep.title, link=ep.link,
                    published=ep.published_str, status="no_transcript",
                ))
                print("    no transcript available -> skipped (logged in email).")
                continue

            print(f"    transcript: {len(transcript):,} chars -> analyzing...")
            analysis = analyze_transcript(
                client, settings.anthropic_model, settings.rules,
                ep.podcast, ep.title, transcript,
            )
            if analysis.error:
                results.append(EpisodeResult(
                    podcast=ep.podcast, title=ep.title, link=ep.link,
                    published=ep.published_str, status="error", note=analysis.error,
                ))
                print(f"    analysis error: {analysis.error}")
                continue

            results.append(EpisodeResult(
                podcast=ep.podcast, title=ep.title, link=ep.link,
                published=ep.published_str, status="analyzed",
                opportunities=analysis.opportunities,
            ))
            print(f"    found {len(analysis.opportunities)} opportunity(ies).")

    # Always persist state so we don't re-analyze next week.
    state.save_seen(seen)

    new_count = sum(1 for r in results)
    if new_count == 0:
        print("\nNothing new this week. No email sent.")
        return 0

    html_body = build_html(results)
    _archive_report(html_body)

    subject = f"📈 每周播客投资机会摘要 · {datetime.now().date()}"
    send_email(
        gmail_address=settings.gmail_address,
        app_password=settings.gmail_app_password,
        to=settings.email_to,
        subject=subject,
        html_body=html_body,
    )
    print(f"\nEmail sent to {settings.email_to} ({new_count} episode(s) reported).")
    return 0


def _archive_report(html_body: str) -> None:
    reports = DATA_DIR / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"{datetime.now().date()}.html"
    path.write_text(html_body, encoding="utf-8")
    print(f"Report archived: {path}")


if __name__ == "__main__":
    sys.exit(run())
