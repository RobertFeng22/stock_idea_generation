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
from .analyze import analyze_transcript, synthesize_picks
from .cache import cache_transcript, get_cached
from .config import DATA_DIR, load_settings
from .emailer import EpisodeResult, build_html, send_email
from .feeds import list_recent_episodes
from .transcribe import transcribe_audio
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
            source = ""

            # 0) Reuse a cached transcript if we've transcribed this episode before.
            cached = get_cached(ep.podcast, ep.guid)
            if cached:
                transcript = cached
                source = "cache"
                print(f"    using cached transcript ({len(cached):,} chars).")

            if not transcript and ep.transcripts:
                transcript = fetch_transcript(ep.transcripts, session)
                if transcript:
                    source = "feed"

            # Fallback: transcribe the audio with Deepgram when the feed has no
            # usable transcript of its own.
            if not transcript and settings.transcription_enabled and ep.audio_url:
                cap = settings.max_transcribe_minutes
                if cap and ep.duration_seconds and ep.duration_seconds > cap * 60:
                    print(f"    audio is {ep.duration_seconds // 60} min > cap "
                          f"{cap} min -> skipped transcription.")
                else:
                    mins = f"~{ep.duration_seconds // 60} min " if ep.duration_seconds else ""
                    print(f"    no feed transcript; transcribing audio {mins}via Deepgram...")
                    transcript = transcribe_audio(
                        ep.audio_url, settings.deepgram_api_key,
                        model=settings.deepgram_model, session=session,
                    )
                    if transcript:
                        source = "deepgram"

            # Persist any freshly obtained transcript so reprocessing is free.
            if transcript and source in ("feed", "deepgram"):
                cache_transcript(ep.podcast, ep.guid, transcript)

            if not transcript:
                note = "无音频链接" if not ep.audio_url else (
                    "转录服务未配置(DEEPGRAM_API_KEY)" if not settings.transcription_enabled
                    else "转录失败")
                results.append(EpisodeResult(
                    podcast=ep.podcast, title=ep.title, link=ep.link,
                    published=ep.published_str, status="no_transcript", note=note,
                ))
                print(f"    no transcript available -> skipped ({note}).")
                continue

            print(f"    transcript [{source}]: {len(transcript):,} chars -> analyzing...")
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
                opportunities=analysis.opportunities, transcript_source=source,
            ))
            print(f"    found {len(analysis.opportunities)} opportunity(ies).")

    # Always persist state so we don't re-analyze next week.
    state.save_seen(seen)

    new_count = sum(1 for r in results)
    if new_count == 0:
        print("\nNothing new this week. No email sent.")
        return 0

    # Stage 2: collapse every raw opportunity into one ranked shortlist (<=10),
    # favouring tickers multiple shows agree on and short (<6mo) catalysts.
    raw_opps = _flatten_opportunities(results)
    synthesis = None
    if raw_opps:
        print(f"\nSynthesizing {len(raw_opps)} raw opportunity(ies) into a shortlist...")
        synthesis = synthesize_picks(client, settings.anthropic_model, settings.rules, raw_opps)
        if synthesis.error:
            print(f"  ! synthesis error: {synthesis.error}")
        else:
            print(f"  -> {len(synthesis.picks)} pick(s) selected.")

    html_body = build_html(results, synthesis)
    _archive_report(html_body)

    subject = f"📈 每周播客投资精选 · {datetime.now().date()}"
    send_email(
        gmail_address=settings.gmail_address,
        app_password=settings.gmail_app_password,
        to=settings.email_to,
        subject=subject,
        html_body=html_body,
    )
    print(f"\nEmail sent to {settings.email_to} ({new_count} episode(s) reported).")
    return 0


def _flatten_opportunities(results) -> list[dict]:
    """Flatten per-episode opportunities into source-tagged dicts for Stage 2."""
    raw = []
    for r in results:
        if r.status != "analyzed":
            continue
        src = f"{r.podcast} — {r.title}"
        for opp in r.opportunities:
            raw.append({
                "source": src,
                "link": r.link,
                "trend": opp.trend,
                "thesis": opp.thesis,
                "tickers": opp.tickers,
                "quote": opp.evidence_quote,
                "horizon": opp.horizon,
                "confidence": opp.confidence,
                "risk": opp.risk,
            })
    return raw


def _archive_report(html_body: str) -> None:
    reports = DATA_DIR / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"{datetime.now().date()}.html"
    path.write_text(html_body, encoding="utf-8")
    print(f"Report archived: {path}")


if __name__ == "__main__":
    sys.exit(run())
