"""
Security master — market cap and index membership for NSE symbols.

Why a committed JSON file rather than a Supabase table:
  * This project's Supabase instance has no `exec_sql` RPC, so
    `database/apply_schema.py` cannot actually create tables — every existing
    table was made by hand in the SQL editor. A new table would mean a manual
    migration step before the report could run at all.
  * Market cap and index membership are *reference* data: slow-moving, small
    (~1.6k rows), and read on every report run. A file read costs nothing and
    removes a network dependency from the report's critical path.
  * Index composition changes become reviewable git diffs — when NSE swaps a
    NIFTY50 constituent, that shows up in a commit rather than silently.

Refreshed by `scrapers/security_master_refresh.py`.

Shape of data/security_master.json:
  {
    "_meta": {"generated_at": "...", "symbols": 1642, "sources": {...}},
    "symbols": {
      "RELIANCE": {
        "name": "Reliance Industries Ltd",
        "isin": "INE002A01018",
        "tier": "NIFTY50",          # NIFTY50 | NIFTY100 | NIFTY500 | null
        "mcap_cr": 1767058.0,
        "cmp": 1306.5,
        "shares": 13525000000,      # derived: mcap_cr * 1e7 / cmp
        "as_of": "2026-07-31"
      },
      ...
    }
  }
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

MASTER_PATH = Path(__file__).resolve().parent.parent / "data" / "security_master.json"

TIER_NIFTY50 = "NIFTY50"
TIER_NIFTY100 = "NIFTY100"
TIER_NIFTY500 = "NIFTY500"

# Rank order for display sorting / tie-breaks (lower = larger index).
_TIER_RANK = {TIER_NIFTY50: 0, TIER_NIFTY100: 1, TIER_NIFTY500: 2}

_cache: dict[str, dict[str, Any]] | None = None
_meta: dict[str, Any] = {}


def _empty() -> dict[str, dict[str, Any]]:
    return {}


def load(force: bool = False) -> dict[str, dict[str, Any]]:
    """Loads the security master, memoised. Returns {} if the file is absent.

    A missing or corrupt file is deliberately non-fatal: the report degrades to
    ranking by deal value rather than failing to send. Callers should check
    `is_loaded()` when the distinction matters.
    """
    global _cache, _meta
    if _cache is not None and not force:
        return _cache

    if not MASTER_PATH.exists():
        logger.warning(
            "Security master not found at %s — market-cap ranking will be "
            "unavailable. Run: python -m scrapers.security_master_refresh",
            MASTER_PATH,
        )
        _cache, _meta = _empty(), {}
        return _cache

    try:
        blob = json.loads(MASTER_PATH.read_text(encoding="utf-8"))
        _cache = blob.get("symbols") or {}
        _meta = blob.get("_meta") or {}
        logger.info(
            "Security master loaded: %d symbols (generated %s)",
            len(_cache), _meta.get("generated_at", "unknown"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Security master unreadable (%s) — continuing without it", exc)
        _cache, _meta = _empty(), {}
    return _cache


def is_loaded() -> bool:
    return bool(load())


def meta() -> dict[str, Any]:
    load()
    return _meta


def get(symbol: str) -> dict[str, Any]:
    return load().get((symbol or "").strip().upper(), {})


def market_cap_cr(symbol: str) -> float | None:
    """Market cap in ₹ crore, or None when unknown."""
    v = get(symbol).get("mcap_cr")
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def company_name(symbol: str) -> str | None:
    return get(symbol).get("name") or None


def last_price(symbol: str) -> float | None:
    """Last known close in ₹, or None. Indicative only — it is the price at the
    last security-master refresh, not the price on any given trade date."""
    v = get(symbol).get("cmp")
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def tier(symbol: str) -> str | None:
    """NIFTY50 / NIFTY100 / NIFTY500, or None if outside the NIFTY500."""
    return get(symbol).get("tier") or None


def is_nifty50(symbol: str) -> bool:
    return tier(symbol) == TIER_NIFTY50


def is_nifty100(symbol: str) -> bool:
    """True for NIFTY50 members too — NIFTY50 is a subset of NIFTY100."""
    return tier(symbol) in (TIER_NIFTY50, TIER_NIFTY100)


def tier_rank(symbol: str) -> int:
    """Sortable index-tier rank; unlisted-in-NIFTY500 sorts last."""
    return _TIER_RANK.get(tier(symbol), 99)


def coverage(symbols: Iterable[str]) -> tuple[int, int]:
    """(n_with_mcap, n_total) — for logging how complete the ranking input is."""
    syms = list(symbols)
    known = sum(1 for s in syms if market_cap_cr(s) is not None)
    return known, len(syms)


def rank_by_market_cap(symbols: Iterable[str], n: int | None = None) -> list[str]:
    """Symbols ordered by market cap, largest first.

    Symbols with no market cap are dropped rather than sorted to the end: an
    unknown-mcap name cannot be asserted to be among the largest, and silently
    padding the list with unknowns would misrepresent the selection. The caller
    logs coverage so a thin master is visible rather than invisible.
    """
    scored = [
        (market_cap_cr(s), s)
        for s in dict.fromkeys(symbols)  # de-dupe, preserve first-seen order
    ]
    scored = [(m, s) for m, s in scored if m is not None]
    # Sort by mcap desc, then symbol asc so equal-mcap ties are deterministic.
    scored.sort(key=lambda t: (-t[0], t[1]))
    out = [s for _, s in scored]
    return out[:n] if n else out


def stale_symbols(symbols: Iterable[str], as_of: date, max_age_days: int) -> list[str]:
    """Symbols whose market cap is missing or older than max_age_days.

    A symbol previously looked up and *not found* (delisted, renamed) carries a
    `checked` date and is skipped until that date ages out too — otherwise the
    long tail of dead tickers in historical deals is re-fetched on every run.
    """
    out = []
    for s in dict.fromkeys(symbols):
        rec = get(s)
        if not rec:
            out.append(s)
            continue

        has_mcap = rec.get("mcap_cr") not in (None, 0)
        stamp = rec.get("as_of") if has_mcap else rec.get("checked")
        if not stamp:
            out.append(s)
            continue
        try:
            age = (as_of - date.fromisoformat(str(stamp))).days
        except (TypeError, ValueError):
            out.append(s)
            continue
        if age > max_age_days:
            out.append(s)
    return out
