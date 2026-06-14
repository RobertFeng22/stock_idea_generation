"""Load configuration: podcast list, analysis rules, and runtime settings."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"


@dataclass
class PodcastEntry:
    name: str | None = None
    apple_url: str | None = None
    feed_url: str | None = None

    def label(self) -> str:
        return self.name or self.feed_url or self.apple_url or "(unnamed podcast)"


@dataclass
class Settings:
    anthropic_api_key: str
    anthropic_model: str
    gmail_address: str
    gmail_app_password: str
    email_to: str
    lookback_days: int
    # Audio transcription fallback (Deepgram). If no key is set, episodes without
    # an in-feed transcript are simply logged as "no transcript" instead.
    deepgram_api_key: str = ""
    deepgram_model: str = "nova-3"
    # Skip transcribing episodes longer than this (0 = no limit). Cost guardrail.
    max_transcribe_minutes: int = 0
    podcasts: list[PodcastEntry] = field(default_factory=list)
    rules: str = ""

    @property
    def transcription_enabled(self) -> bool:
        return bool(self.deepgram_api_key)


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise SystemExit(
            f"Missing required environment variable: {name}. "
            f"Set it in your .env file (local) or repository Secrets (GitHub Actions)."
        )
    return val


def load_podcasts() -> list[PodcastEntry]:
    path = CONFIG_DIR / "podcasts.yaml"
    if not path.exists():
        raise SystemExit(f"Podcast config not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = []
    for item in data.get("podcasts", []) or []:
        if not item:
            continue
        entries.append(
            PodcastEntry(
                name=item.get("name"),
                apple_url=item.get("apple_url"),
                feed_url=item.get("feed_url"),
            )
        )
    return entries


def load_rules() -> str:
    path = CONFIG_DIR / "rules.md"
    if not path.exists():
        raise SystemExit(f"Rules file not found: {path}")
    return path.read_text(encoding="utf-8")


def _env(name: str, default: str = "") -> str:
    """Like os.environ.get but treats an empty/whitespace value as unset.

    GitHub Actions passes unset optional secrets as empty strings, which would
    otherwise override our defaults (e.g. an empty ANTHROPIC_MODEL).
    """
    return (os.environ.get(name) or "").strip() or default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except (ValueError, TypeError):
        return default


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    gmail = _require("GMAIL_ADDRESS")
    return Settings(
        anthropic_api_key=_require("ANTHROPIC_API_KEY"),
        anthropic_model=_env("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        gmail_address=gmail,
        gmail_app_password=_require("GMAIL_APP_PASSWORD"),
        email_to=_env("EMAIL_TO") or gmail,
        lookback_days=_env_int("LOOKBACK_DAYS", 7),
        deepgram_api_key=_env("DEEPGRAM_API_KEY"),
        deepgram_model=_env("DEEPGRAM_MODEL", "nova-3"),
        max_transcribe_minutes=_env_int("MAX_TRANSCRIBE_MINUTES", 0),
        podcasts=load_podcasts(),
        rules=load_rules(),
    )
