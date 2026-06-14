"""Audio transcription fallback via Deepgram.

Used when a podcast episode does NOT ship a transcript in its RSS feed. Deepgram
fetches the audio URL server-side (so we never download large MP3s on the
runner) and returns a formatted transcript.

Docs: https://developers.deepgram.com/reference/listen-remote
"""
from __future__ import annotations

import requests

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"


def transcribe_audio(
    audio_url: str,
    api_key: str,
    model: str = "nova-3",
    timeout: int = 900,
    session: requests.Session | None = None,
) -> str | None:
    """Transcribe a remote audio URL. Returns plain text, or None on failure."""
    sess = session or requests.Session()
    params = {
        "model": model,
        "smart_format": "true",  # punctuation, paragraphs, numerals
        "punctuate": "true",
        "paragraphs": "true",
        "detect_language": "true",
    }
    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type": "application/json",
    }
    try:
        resp = sess.post(
            DEEPGRAM_URL,
            params=params,
            headers=headers,
            json={"url": audio_url},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        print(f"    ! Deepgram transcription failed: {exc}")
        return None

    return _extract_transcript(data)


def _extract_transcript(data: dict) -> str | None:
    try:
        alt = data["results"]["channels"][0]["alternatives"][0]
    except (KeyError, IndexError, TypeError):
        return None

    # Prefer the paragraph-formatted transcript when available.
    para = alt.get("paragraphs", {})
    if isinstance(para, dict) and para.get("transcript"):
        text = para["transcript"].strip()
    else:
        text = (alt.get("transcript") or "").strip()

    return text if len(text) > 200 else None
