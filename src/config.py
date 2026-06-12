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
    podcasts: list[PodcastEntry] = field(default_factory=list)
    rules: str = ""


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


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    gmail = _require("GMAIL_ADDRESS")
    return Settings(
        anthropic_api_key=_require("ANTHROPIC_API_KEY"),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6").strip(),
        gmail_address=gmail,
        gmail_app_password=_require("GMAIL_APP_PASSWORD"),
        email_to=os.environ.get("EMAIL_TO", "").strip() or gmail,
        lookback_days=int(os.environ.get("LOOKBACK_DAYS", "7")),
        podcasts=load_podcasts(),
        rules=load_rules(),
    )
