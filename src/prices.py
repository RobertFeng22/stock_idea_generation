"""Daily close prices for evaluation, with an append-only on-disk cache.

Primary source is Stooq's free CSV endpoint; Yahoo Finance's chart API is the
fallback (covers OTC ADRs like NTDOY that Stooq may miss). Once a (ticker, date)
close is recorded in the cache it is never overwritten, so entry prices are
pinned at the first weekly run that observes them even if a source later
revises or drops history.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import date

import requests

from .config import DATA_DIR

EVAL_DIR = DATA_DIR / "evaluation"
PRICES_PATH = EVAL_DIR / "prices.json"

STOOQ_URL = "https://stooq.com/q/d/l/?s={symbol}.us&i=d&d1={d1}&d2={d2}"
YAHOO_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    "?period1={p1}&period2={p2}&interval=1d&events=div%2Csplit"
)
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}


def load_cache() -> dict[str, dict[str, float]]:
    if PRICES_PATH.exists():
        return json.loads(PRICES_PATH.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict[str, dict[str, float]]) -> None:
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    PRICES_PATH.write_text(
        json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8"
    )


def _fetch_stooq(symbol: str, start: date, end: date,
                 session: requests.Session) -> dict[str, float]:
    url = STOOQ_URL.format(symbol=symbol.lower(),
                           d1=start.strftime("%Y%m%d"), d2=end.strftime("%Y%m%d"))
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    out: dict[str, float] = {}
    reader = csv.DictReader(io.StringIO(resp.text))
    for row in reader:
        try:
            out[row["Date"]] = float(row["Close"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _fetch_yahoo(symbol: str, start: date, end: date,
                 session: requests.Session) -> dict[str, float]:
    from datetime import datetime, timedelta, timezone

    p1 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    p2 = int((datetime(end.year, end.month, end.day, tzinfo=timezone.utc)
              + timedelta(days=1)).timestamp())
    url = YAHOO_URL.format(symbol=symbol, p1=p1, p2=p2)
    resp = session.get(url, timeout=30, headers=YAHOO_HEADERS)
    resp.raise_for_status()
    data = resp.json()
    result = (data.get("chart", {}).get("result") or [None])[0]
    if not result:
        return {}
    stamps = result.get("timestamp") or []
    closes = (result.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    out: dict[str, float] = {}
    for ts, close in zip(stamps, closes):
        if close is None:
            continue
        day = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        out[day] = round(float(close), 4)
    return out


def fetch_series(symbol: str, start: date, end: date,
                 session: requests.Session | None = None) -> dict[str, float]:
    """Daily closes for symbol between start and end, from any working source."""
    session = session or requests.Session()
    for fetcher in (_fetch_stooq, _fetch_yahoo):
        try:
            series = fetcher(symbol, start, end, session)
            if series:
                return series
        except Exception as exc:  # noqa: BLE001 - source failures are expected
            print(f"    {fetcher.__name__} failed for {symbol}: {exc}")
    return {}


def ensure_prices(symbols: list[str], start: date, end: date) -> dict[str, dict[str, float]]:
    """Update the cache with fresh closes for every symbol; return the cache.

    New observations are merged in; existing (ticker, date) entries are kept
    as-is (append-only) so previously pinned prices never silently change.
    """
    cache = load_cache()
    session = requests.Session()
    for symbol in symbols:
        fresh = fetch_series(symbol, start, end, session)
        if not fresh:
            print(f"  ! no price data for {symbol}")
            continue
        held = cache.setdefault(symbol, {})
        added = 0
        for day, close in fresh.items():
            if day not in held:
                held[day] = close
                added += 1
        print(f"  {symbol}: +{added} day(s), {len(held)} total")
    save_cache(cache)
    return cache
