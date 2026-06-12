"""Use Claude to mine investment opportunities from a transcript."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from anthropic import Anthropic

# Keep cost predictable: cap how much transcript text we send per episode.
# Roughly 240k chars ~= 60k tokens. Long shows get truncated with a note.
MAX_TRANSCRIPT_CHARS = 240_000

SYSTEM_PROMPT = """You are an equity research analyst assistant. You read a \
podcast transcript and surface potential US-stock investment ideas, strictly \
following the user's rules. You are skeptical and precise: prefer returning an \
empty list over low-quality, generic ideas. Every idea must be grounded in \
something actually said in the transcript.

Return ONLY a JSON object, no prose, matching this schema:
{
  "opportunities": [
    {
      "trend": "short name of the trend/phenomenon",
      "thesis": "1-3 sentences: because X, Y should benefit",
      "tickers": [
        {"ticker": "AAPL", "name": "Apple Inc.", "rationale": "how it benefits"}
      ],
      "evidence_quote": "a short verbatim quote from the transcript that supports this",
      "confidence": "high|medium|low",
      "risk": "the main counter-argument or risk"
    }
  ]
}
If there are no high-quality ideas, return {"opportunities": []}."""


@dataclass
class Opportunity:
    trend: str
    thesis: str
    tickers: list[dict]
    evidence_quote: str
    confidence: str
    risk: str


@dataclass
class EpisodeAnalysis:
    opportunities: list[Opportunity] = field(default_factory=list)
    error: str | None = None


def analyze_transcript(
    client: Anthropic,
    model: str,
    rules: str,
    podcast: str,
    title: str,
    transcript: str,
) -> EpisodeAnalysis:
    truncated = transcript[:MAX_TRANSCRIPT_CHARS]
    note = "" if len(transcript) <= MAX_TRANSCRIPT_CHARS else "\n\n[Transcript truncated for length.]"

    user_msg = f"""## My investment rules
{rules}

## Podcast
Show: {podcast}
Episode: {title}

## Transcript
{truncated}{note}

Following my rules above, extract the investment opportunities as JSON."""

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as exc:  # noqa: BLE001
        return EpisodeAnalysis(error=str(exc))

    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    data = _extract_json(text)
    if data is None:
        return EpisodeAnalysis(error="Could not parse model JSON output.")

    opps = []
    for o in data.get("opportunities", []) or []:
        opps.append(
            Opportunity(
                trend=str(o.get("trend", "")).strip(),
                thesis=str(o.get("thesis", "")).strip(),
                tickers=[t for t in (o.get("tickers") or []) if isinstance(t, dict)],
                evidence_quote=str(o.get("evidence_quote", "")).strip(),
                confidence=str(o.get("confidence", "")).strip().lower(),
                risk=str(o.get("risk", "")).strip(),
            )
        )
    return EpisodeAnalysis(opportunities=opps)


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
