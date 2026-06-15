"""Two-stage analysis with Claude.

Stage 1 (``analyze_transcript``): read one episode's transcript and extract raw
investment opportunities grounded in quotes.

Stage 2 (``synthesize_picks``): take every raw opportunity found across the whole
week and produce a single ranked shortlist of up to 10 stocks — favouring names
that several different episodes/shows point at, and catalysts that play out
within ~6 months. Each pick carries supporting quotes, a risk note, and a
bull-vs-bear debate with a rebuttal.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from anthropic import Anthropic

# Keep cost predictable: cap how much transcript text we send per episode.
# Roughly 240k chars ~= 60k tokens. Long shows get truncated with a note.
MAX_TRANSCRIPT_CHARS = 240_000

MAX_PICKS = 10

# ---------------------------------------------------------------------------
# Stage 1: per-episode extraction
# ---------------------------------------------------------------------------

EXTRACT_SYSTEM_PROMPT = """You are an equity research analyst assistant. You read \
a podcast transcript and surface potential US-stock investment ideas, strictly \
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
      "horizon": "when the catalyst likely plays out, e.g. '<3 months', '3-6 months', '6-12 months', '>12 months'",
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
    horizon: str = ""


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
            system=EXTRACT_SYSTEM_PROMPT,
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
                horizon=str(o.get("horizon", "")).strip(),
                confidence=str(o.get("confidence", "")).strip().lower(),
                risk=str(o.get("risk", "")).strip(),
            )
        )
    return EpisodeAnalysis(opportunities=opps)


# ---------------------------------------------------------------------------
# Stage 2: weekly synthesis into a ranked shortlist
# ---------------------------------------------------------------------------

SYNTH_SYSTEM_PROMPT = f"""You are a portfolio strategist. You are given a list of \
raw investment opportunities that an analyst extracted from this week's podcasts \
(each item notes which show/episode it came from, with a supporting quote).

Consolidate them into a single ranked shortlist of AT MOST {MAX_PICKS} US-listed \
stocks. Selection criteria:

REQUIRED FILTER — NOT YET PRICED IN: only include a stock if you can give a \
CREDIBLE reason the broad market has NOT yet priced this trend into the stock — \
i.e. it is still a non-consensus / under-the-radar insight, not common knowledge. \
Note that several of these podcasts discussing a theme does NOT mean the market \
has priced it in: these are informed/niche investor sources, and the beneficiary \
may be a second-order or overlooked name, the implication may be misunderstood, \
or it may simply be too early. If a name is already obvious, widely covered by \
mainstream financial media, and clearly reflected in the price, DROP it. State \
the mispricing reason explicitly in 'why_not_priced'.

Ranking priorities, in order:
1. CONVERGENCE: strongly prefer tickers that MULTIPLE DIFFERENT episodes or shows \
independently point to as beneficiaries. The more independent sources, the higher \
the rank.
2. NON-CONSENSUS edge: the stronger and more specific the reason it is not yet \
priced in, the higher the rank.
3. SHORT HORIZON: prefer catalysts likely to play out within ~6 months.
4. Strength and specificity of the evidence.

Deduplicate by ticker (merge everything said about the same company). Returning \
fewer than {MAX_PICKS} is fine — quality and a real non-consensus edge over \
quantity. Rank best first.

For EACH pick, write a genuine bull-vs-bear debate: the strongest bear argument, \
then the bull's rebuttal to it. Keep quotes verbatim from the input.

Return ONLY a JSON object, no prose, matching this schema:
{{
  "summary": "1-2 sentence overview of this week's strongest theme",
  "picks": [
    {{
      "ticker": "NVDA",
      "name": "NVIDIA Corp.",
      "thesis": "why this benefits, 1-3 sentences",
      "horizon": "e.g. '3-6 months'",
      "conviction": "high|medium|low",
      "why_not_priced": "concrete reason the market has NOT yet priced this in (e.g. overlooked second-order beneficiary, misunderstood implication, too early, no sell-side coverage)",
      "sources": ["Show A — episode title", "Show B — episode title"],
      "supporting_points": [
        {{"point": "the observation", "quote": "verbatim quote", "source": "Show A — episode title"}}
      ],
      "risk": "key risk / what would break the thesis",
      "bull_case": "the strongest argument FOR",
      "bear_case": "the strongest argument AGAINST",
      "rebuttal": "the bull's response to the bear case"
    }}
  ]
}}
If nothing qualifies, return {{"summary": "", "picks": []}}."""


@dataclass
class Pick:
    ticker: str
    name: str
    thesis: str
    horizon: str
    conviction: str
    why_not_priced: str
    sources: list[str]
    supporting_points: list[dict]  # [{point, quote, source}]
    risk: str
    bull_case: str
    bear_case: str
    rebuttal: str


@dataclass
class Synthesis:
    summary: str = ""
    picks: list[Pick] = field(default_factory=list)
    error: str | None = None


def synthesize_picks(
    client: Anthropic,
    model: str,
    rules: str,
    raw_opportunities: list[dict],
) -> Synthesis:
    """Collapse all of the week's raw opportunities into a ranked shortlist."""
    if not raw_opportunities:
        return Synthesis()

    payload = json.dumps(raw_opportunities, ensure_ascii=False, indent=2)
    user_msg = f"""## My investment rules
{rules}

## Raw opportunities extracted from this week's podcasts (JSON)
{payload}

Consolidate these into the ranked shortlist as specified."""

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=16000,
            system=SYNTH_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as exc:  # noqa: BLE001
        return Synthesis(error=str(exc))

    stop = getattr(resp, "stop_reason", None)
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    data = _extract_json(text)
    if data is None:
        # The model may have been truncated mid-JSON; salvage complete picks.
        data = _salvage_synthesis(text)
    if data is None:
        hint = " (output hit max_tokens)" if stop == "max_tokens" else ""
        return Synthesis(error=f"Could not parse synthesis JSON output.{hint}")

    picks = []
    for p in (data.get("picks") or [])[:MAX_PICKS]:
        if not isinstance(p, dict):
            continue
        picks.append(
            Pick(
                ticker=str(p.get("ticker", "")).strip().upper(),
                name=str(p.get("name", "")).strip(),
                thesis=str(p.get("thesis", "")).strip(),
                horizon=str(p.get("horizon", "")).strip(),
                conviction=str(p.get("conviction", "")).strip().lower(),
                why_not_priced=str(p.get("why_not_priced", "")).strip(),
                sources=[str(s).strip() for s in (p.get("sources") or []) if str(s).strip()],
                supporting_points=[
                    sp for sp in (p.get("supporting_points") or []) if isinstance(sp, dict)
                ],
                risk=str(p.get("risk", "")).strip(),
                bull_case=str(p.get("bull_case", "")).strip(),
                bear_case=str(p.get("bear_case", "")).strip(),
                rebuttal=str(p.get("rebuttal", "")).strip(),
            )
        )
    return Synthesis(summary=str(data.get("summary", "")).strip(), picks=picks)


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


def _salvage_synthesis(text: str) -> dict | None:
    """Recover a synthesis object from output that was truncated mid-JSON.

    Walks the "picks" array and keeps every fully-closed object, discarding a
    trailing incomplete one. Returns None if nothing usable is found.
    """
    summary = ""
    msum = re.search(r'"summary"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if msum:
        try:
            summary = json.loads(f'"{msum.group(1)}"')
        except json.JSONDecodeError:
            summary = msum.group(1)

    key = text.find('"picks"')
    if key == -1:
        return None
    start = text.find("[", key)
    if start == -1:
        return None

    picks: list = []
    depth = 0
    obj_start = None
    in_str = False
    esc = False
    for j in range(start + 1, len(text)):
        c = text[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            if depth == 0:
                obj_start = j
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and obj_start is not None:
                try:
                    picks.append(json.loads(text[obj_start : j + 1]))
                except json.JSONDecodeError:
                    pass
                obj_start = None
        elif c == "]" and depth == 0:
            break

    if not picks:
        return None
    return {"summary": summary, "picks": picks}
