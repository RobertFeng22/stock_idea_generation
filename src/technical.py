"""Per-stock technical analysis module.

This is intentionally a placeholder for now. The user will provide the technical
analysis framework later; until then ``is_configured`` returns False and the
email shows a reserved "技术分析（待补充）" block for each pick.

When the framework is provided (config/technical_rules.md no longer contains the
placeholder marker), this module will:
  1. fetch recent price / OHLCV data for the ticker (data source TBD), and
  2. ask the model to apply the user's framework and return an analysis.
Both steps are stubbed below and clearly marked with TODO.
"""
from __future__ import annotations

PLACEHOLDER_MARKER = "TECHNICAL_RULES_PLACEHOLDER"

# Shown in the email when no framework has been provided yet.
PLACEHOLDER_TEXT = "（技术分析框架待补充：在 config/technical_rules.md 填入框架后，这里会自动生成）"


def is_configured(technical_rules: str) -> bool:
    """True once the user has replaced the placeholder with a real framework."""
    if not technical_rules:
        return False
    if PLACEHOLDER_MARKER in technical_rules:
        return False
    # Require some substantive content (not just headings/comments).
    meaningful = [
        ln for ln in technical_rules.splitlines()
        if ln.strip() and not ln.lstrip().startswith(("#", ">", "<!--", "-"))
    ]
    return len("".join(meaningful).strip()) > 40


def build_technical_analysis(client, model: str, technical_rules: str, pick) -> str:
    """Return a technical-analysis string for one pick (empty = use placeholder).

    Currently inert. Wiring is in place so the only change needed once the
    framework arrives is to implement the two TODO steps below.
    """
    if not is_configured(technical_rules):
        return ""  # emailer renders PLACEHOLDER_TEXT instead

    # TODO(when framework provided):
    #   prices = fetch_price_data(pick.ticker)            # OHLCV from chosen source
    #   prompt = f"Apply this framework:\n{technical_rules}\n\nTicker: {pick.ticker}\n{prices}"
    #   resp = client.messages.create(model=model, max_tokens=1500, messages=[...])
    #   return extracted_text
    return ""
