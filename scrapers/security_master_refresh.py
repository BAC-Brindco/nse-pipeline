"""
Refreshes data/security_master.json — index membership + market cap.

Two independent sources, each degrading on its own:

  1. Index membership — NSE archive CSVs (nsearchives.nseindia.com):
       ind_nifty50list.csv / ind_nifty100list.csv / ind_nifty500list.csv
     These are static CDN files and answer reliably even when NSE's JSON
     APIs 403 (which they do from most cloud runners). Membership is
     re-derived from scratch each run so index reshuffles propagate — a name
     dropped from the NIFTY50 loses the tier rather than keeping it forever.

  2. Market cap — Screener.in company pages.
     NSE's own market cap lives behind /api/quote-equity, which is
     Akamai-gated and 403s without a browser-grade session; there is no bulk
     archive CSV carrying shares outstanding. Screener.in serves both Market
     Cap and Current Price as plain server-rendered HTML, from which shares
     outstanding falls out (mcap / price) — so a stale price can be
     re-multiplied later without re-scraping.

Usage:
  python -m scrapers.security_master_refresh                  # indices + stale mcaps
  python -m scrapers.security_master_refresh --indices-only   # skip Screener
  python -m scrapers.security_master_refresh --symbols RELIANCE TCS
  python -m scrapers.security_master_refresh --from-deals     # every symbol ever dealt
  python -m scrapers.security_master_refresh --limit 200      # bound one run
  python -m scrapers.security_master_refresh --max-age-days 30
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

import pandas as pd

from utils.security_master import (
    MASTER_PATH,
    TIER_NIFTY50,
    TIER_NIFTY100,
    TIER_NIFTY500,
    load,
    stale_symbols,
)

logger = logging.getLogger("nse.security_master")

_INDEX_FILES = [
    # Narrowest first — a later, broader file must not overwrite a tighter tier.
    (TIER_NIFTY50, "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"),
    (TIER_NIFTY100, "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv"),
    (TIER_NIFTY500, "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"),
]

_SCREENER_URL = "https://www.screener.in/company/{symbol}/consolidated/"
_SCREENER_URL_STANDALONE = "https://www.screener.in/company/{symbol}/"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Screener renders each top-ratio as:
#   <span class="name"> Market Cap </span> ... <span class="number">17,67,058</span>
# The label and the number are separated by markup, so match across it but stop
# at the next <li> to avoid running into the following ratio.
_RATIO_RX = (
    r'<span class="name">\s*{label}\s*</span>.*?'
    r'<span class="number">\s*([\d,\.]+)\s*</span>'
)

_MCAP_RX = re.compile(_RATIO_RX.format(label="Market Cap"), re.S)
_CMP_RX = re.compile(_RATIO_RX.format(label="Current Price"), re.S)

# Politeness: Screener is a free service and we are a batch consumer.
_DELAY_MIN = 0.8
_DELAY_MAX = 1.6
_MAX_CONSECUTIVE_FAILURES = 12


def _num(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# ─── Index membership ─────────────────────────────────────────────────────────

def _fetch_index_lists(session) -> dict[str, dict[str, str]]:
    """{symbol: {tier, name, isin}} — narrowest tier wins."""
    out: dict[str, dict[str, str]] = {}
    for tier, url in _INDEX_FILES:
        try:
            resp = session.get(url)
            df = pd.read_csv(io.StringIO(resp.text))
            df.columns = [c.strip() for c in df.columns]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Index list %s failed (%s) — tier left unchanged", tier, str(exc)[:120])
            continue

        n_new = 0
        for _, row in df.iterrows():
            sym = str(row.get("Symbol", "")).strip().upper()
            if not sym or sym in ("NAN", "NONE"):
                continue
            if sym in out:  # already claimed by a narrower index
                continue
            out[sym] = {
                "tier": tier,
                "name": str(row.get("Company Name", "")).strip() or None,
                "isin": str(row.get("ISIN Code", "")).strip() or None,
            }
            n_new += 1
        logger.info("Index %s: %d rows, %d newly tiered", tier, len(df), n_new)
    return out


# ─── Market cap (Screener.in) ────────────────────────────────────────────────

def _screener_get(url: str, timeout: int = 20) -> str | None:
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _fetch_market_cap(symbol: str) -> dict | None:
    """{mcap_cr, cmp, shares} for one symbol, or None if unavailable.

    Tries the consolidated page first (what the desk quotes), then standalone —
    companies without subsidiaries have no /consolidated/ page and 404 there.
    """
    for tmpl in (_SCREENER_URL, _SCREENER_URL_STANDALONE):
        url = tmpl.format(symbol=urllib.parse.quote(symbol, safe=""))
        try:
            html = _screener_get(url)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Screener %s errored: %s", symbol, str(exc)[:100])
            return None
        if html is None:
            continue  # 404 → try the other page shape

        mcap = _num(m.group(1) if (m := _MCAP_RX.search(html)) else None)
        cmp_ = _num(m.group(1) if (m := _CMP_RX.search(html)) else None)
        if not mcap:
            continue

        shares = None
        if cmp_ and cmp_ > 0:
            # mcap is in ₹ crore; 1 crore = 1e7 rupees.
            shares = int(round(mcap * 1e7 / cmp_))
        return {"mcap_cr": mcap, "cmp": cmp_, "shares": shares}
    return None


def _refresh_market_caps(
    symbols: list[str], existing: dict, today: date,
    flush: "callable | None" = None, flush_every: int = 100,
) -> tuple[int, int]:
    """Fetches market caps, checkpointing via `flush` so a long seeding run
    interrupted at symbol 1,300 of 1,432 keeps everything fetched so far."""
    ok = fail = 0
    consecutive = 0
    for i, sym in enumerate(symbols, 1):
        rec = _fetch_market_cap(sym)
        if rec:
            existing.setdefault(sym, {}).update({
                "mcap_cr": rec["mcap_cr"],
                "cmp": rec["cmp"],
                "shares": rec["shares"],
                "as_of": today.isoformat(),
                "mcap_source": "screener.in",
            })
            ok += 1
            consecutive = 0
        else:
            # Record the miss rather than leaving no key. Most misses are
            # delisted or renamed tickers still present in historical deals;
            # without a negative record every refresh re-fetches all of them
            # forever. `checked` lets stale_symbols skip them for a while.
            existing.setdefault(sym, {}).update({
                "mcap_cr": None,
                "checked": today.isoformat(),
                "mcap_source": "screener.in:not_found",
            })
            fail += 1
            consecutive += 1
            logger.debug("No market cap for %s", sym)

        if i % 50 == 0 or i == len(symbols):
            logger.info("Market cap: %d/%d done (%d ok, %d miss)", i, len(symbols), ok, fail)

        if flush and i % flush_every == 0:
            flush()
            logger.info("Checkpointed at %d/%d", i, len(symbols))

        # A long unbroken failure run means we are blocked or rate-limited, not
        # that 12 companies in a row are delisted. Stop rather than hammer on
        # and write a file full of holes.
        if consecutive >= _MAX_CONSECUTIVE_FAILURES:
            logger.warning(
                "Aborting market-cap refresh after %d consecutive failures "
                "(likely rate-limited) — %d symbols left unrefreshed",
                consecutive, len(symbols) - i,
            )
            break

        time.sleep(random.uniform(_DELAY_MIN, _DELAY_MAX))
    return ok, fail


# ─── Deal-symbol discovery ───────────────────────────────────────────────────

def _symbols_from_deals() -> list[str]:
    """Every symbol that has ever appeared in bulk / block / short deals."""
    from database.client import get_client
    client = get_client()
    found: set[str] = set()
    for table in ("bulk_deals", "block_deals", "short_deals"):
        page = 0
        while True:
            resp = (
                client.table(table).select("symbol")
                .range(page * 1000, (page + 1) * 1000 - 1)
                .execute()
            )
            rows = resp.data or []
            found |= {
                str(r["symbol"]).strip().upper()
                for r in rows if r.get("symbol")
            }
            if len(rows) < 1000:
                break
            page += 1
        logger.info("Deal symbols after %s: %d", table, len(found))
    return sorted(found)


# ─── Entry point ─────────────────────────────────────────────────────────────

def refresh(
    *,
    indices_only: bool = False,
    symbols: list[str] | None = None,
    from_deals: bool = False,
    limit: int | None = None,
    max_age_days: int = 30,
) -> dict:
    from scrapers.nse_session import NSESession
    from utils.helpers import today_ist

    today = date.fromisoformat(today_ist())
    master = dict(load(force=True))  # existing records, mutated in place

    # 1) Index membership — always refreshed, cheap and authoritative.
    session = NSESession()
    tiers = _fetch_index_lists(session)
    if tiers:
        # Clear stale tiers first so a dropped constituent actually loses its
        # badge; re-apply from the freshly fetched lists.
        for rec in master.values():
            rec.pop("tier", None)
        for sym, info in tiers.items():
            rec = master.setdefault(sym, {})
            rec["tier"] = info["tier"]
            if info.get("name"):
                rec["name"] = info["name"]
            if info.get("isin"):
                rec["isin"] = info["isin"]
        logger.info("Index membership applied to %d symbols", len(tiers))
    else:
        # Tiers are only cleared inside the branch above, so a total fetch
        # failure leaves the previous membership intact rather than wiping every
        # badge off the report.
        logger.warning("No index lists fetched — existing tiers left as-is")

    def _write() -> dict:
        MASTER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with_mcap = sum(1 for r in master.values() if r.get("mcap_cr"))
        blob = {
            "_meta": {
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "symbols": len(master),
                "with_market_cap": with_mcap,
                "nifty50": sum(1 for r in master.values() if r.get("tier") == TIER_NIFTY50),
                "nifty100": sum(1 for r in master.values() if r.get("tier") == TIER_NIFTY100),
                "nifty500": sum(1 for r in master.values() if r.get("tier") == TIER_NIFTY500),
                "sources": {
                    "index_membership": "nsearchives.nseindia.com/content/indices",
                    "market_cap": "screener.in",
                },
            },
            # Sorted so the committed file diffs cleanly run to run.
            "symbols": {k: master[k] for k in sorted(master)},
        }
        # Write via a temp file + replace so an interrupted flush cannot leave
        # a half-written JSON file that the report then fails to parse.
        tmp = MASTER_PATH.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(blob, indent=1, ensure_ascii=False, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        tmp.replace(MASTER_PATH)
        return {"symbols": len(master), "with_market_cap": with_mcap}

    # 2) Market cap.
    n_ok = n_fail = 0
    if not indices_only:
        if symbols:
            targets = [s.strip().upper() for s in symbols if s.strip()]
        else:
            pool = set(master.keys())
            if from_deals:
                pool |= set(_symbols_from_deals())
            targets = stale_symbols(sorted(pool), today, max_age_days)
        if limit:
            targets = targets[:limit]
        logger.info("Market cap refresh targets: %d symbols", len(targets))
        if targets:
            n_ok, n_fail = _refresh_market_caps(targets, master, today, flush=_write)

    # 3) Write.
    written = _write()
    result = {
        **written,
        "mcap_refreshed": n_ok,
        "mcap_missing": n_fail,
        "path": str(MASTER_PATH),
    }
    logger.info("Security master written: %s", result)
    return result


def _parse_args():
    p = argparse.ArgumentParser(description="Refresh security master (index tiers + market cap)")
    p.add_argument("--indices-only", action="store_true",
                   help="Refresh index membership only; skip Screener.in")
    p.add_argument("--symbols", nargs="+", metavar="SYM",
                   help="Refresh market cap for these symbols only")
    p.add_argument("--from-deals", action="store_true",
                   help="Include every symbol ever seen in the deals tables")
    p.add_argument("--limit", type=int,
                   help="Cap symbols fetched this run (for incremental seeding)")
    p.add_argument("--max-age-days", type=int, default=30,
                   help="Re-fetch market caps older than this (default 30)")
    return p.parse_args()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    args = _parse_args()
    from dotenv import load_dotenv
    load_dotenv()
    refresh(
        indices_only=args.indices_only,
        symbols=args.symbols,
        from_deals=args.from_deals,
        limit=args.limit,
        max_age_days=args.max_age_days,
    )
