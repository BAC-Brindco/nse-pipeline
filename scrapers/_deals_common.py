"""
Shared logic for bulk-deals + block-deals scrapers.

Both endpoints share an identical contract:
  range API:    https://www.nseindia.com/api/historical/cm/{kind}?from=DD-MM-YYYY&to=DD-MM-YYYY
  daily snap:   https://www.nseindia.com/api/snapshot-capital-market-largedeal
                  → keys BULK_DEALS_DATA / BLOCK_DEALS_DATA
  archive CSV:  https://archives.nseindia.com/content/equities/{KIND}_DEALS_{DD-Mon-YYYY}.csv

The daily flow uses the snapshot. Backfill walks the range API in
quarterly chunks; the archive CSV is the fallback for windows where
the API returns nothing (older years).

Quarterly windowing rationale: NSE has historically allowed up to ~365
days per call but throttles aggressively above 90 — 90 stays inside the
fast path and keeps response payloads under ~5 MB.
"""

from __future__ import annotations

import io
import logging
from datetime import date, timedelta
from typing import Callable

import pandas as pd

from scrapers.nse_session import NSESession
from database.client import bulk_upsert, set_checkpoint
from utils.helpers import (
    clean_str, clean_date, clean_numeric, clean_int,
    buy_sell_flag, today_ist, iter_trading_days,
)
from config import NSE_ARCHIVE_URL

logger = logging.getLogger(__name__)

WINDOW_DAYS = 90  # quarterly chunks


def _api_date(d: date) -> str:
    return d.strftime("%d-%m-%Y")


def _historical_url(kind: str, from_dt: date, to_dt: date) -> str:
    return (
        f"https://www.nseindia.com/api/historical/cm/{kind}"
        f"?from={_api_date(from_dt)}&to={_api_date(to_dt)}"
    )


class HtmlChallengeError(RuntimeError):
    """Raised when NSE returns an HTML bot-challenge page instead of JSON.
    Signals broader auth failure — fallback to archive CDN won't help either."""


def fetch_range(session: NSESession, kind: str, from_dt: date, to_dt: date) -> list[dict]:
    """Fetch a date range from NSE's historical API. Returns raw rows.

    Raises HtmlChallengeError if NSE served the bot-challenge HTML page —
    callers should skip the archive-CDN fallback in that case (cookies
    aren't authenticated enough for /content/equities/ either).
    """
    url = _historical_url(kind, from_dt, to_dt)
    # NSE's Akamai stack on /api/historical/cm/* checks Referer matches
    # the historical-deals UI page. Without it we get the bot-challenge HTML.
    headers = {
        "Referer": "https://www.nseindia.com/report-detail/historical-bulk-deals",
    }
    try:
        payload = session.get_json(url, headers=headers)
    except ValueError as exc:
        # Non-JSON response — most likely the bot-challenge HTML page.
        msg = str(exc)
        if "<!DOCTYPE html" in msg or "<html" in msg.lower():
            raise HtmlChallengeError(
                f"{kind} historical API blocked by NSE bot challenge"
            ) from exc
        logger.warning("Historical %s [%s → %s] non-JSON: %s",
                       kind, from_dt, to_dt, msg[:200])
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Historical %s [%s → %s] failed: %s", kind, from_dt, to_dt, exc)
        return []
    raw = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        return []
    return raw


def archive_urls(d: date, kind: str) -> list[str]:
    """Possible CDN paths for a single day's CSV. Naming has changed over years."""
    mon3 = d.strftime("%b").capitalize()
    ddmmyyyy = d.strftime("%d%m%Y")
    ddmonyyyy = d.strftime("%d-") + mon3 + d.strftime("-%Y")  # 01-Jan-2023

    upper = kind.upper()
    return [
        f"{NSE_ARCHIVE_URL}/content/equities/{upper}_{ddmonyyyy}.csv",
        f"{NSE_ARCHIVE_URL}/content/equities/{kind}_{ddmmyyyy}.csv",
        f"{NSE_ARCHIVE_URL}/content/equities/{upper}_{ddmmyyyy}.csv",
    ]


def fetch_archive_csv(session: NSESession, d: date, kind: str) -> tuple[pd.DataFrame | None, str | None]:
    """Returns (df, source_url) or (None, None) if no CDN path responds."""
    for url in archive_urls(d, kind):
        try:
            resp = session.get(url)
            df = pd.read_csv(io.StringIO(resp.text))
            if df.empty:
                continue
            return df, url
        except Exception as exc:  # noqa: BLE001
            logger.debug("Archive %s failed: %s", url, exc)
    return None, None


def run_historical_backfill(
    *,
    dataset_key: str,           # 'bulk_deals' | 'block_deals'
    api_kind: str,              # 'bulk_deals' | 'block_deals'  (NSE URL segment)
    table: str,
    conflict_columns: list[str],
    parse_api_row: Callable[[dict, str, str], dict],
    parse_csv_row: Callable[[pd.Series, date, str, str], dict],
    session: NSESession,
    start: date,
    end: date,
) -> dict:
    """
    Walks [start, end] in 90-day windows. For each window:
      1. Try the range API.
      2. For trading days the API returned nothing for, try the archive CSV.
    Persists data_source + source_url for every row.
    Updates backfill_checkpoint after each window completes successfully.
    """
    scrape_date = today_ist()
    total_fetched = total_upserted = 0

    cursor = start
    consecutive_html_blocks = 0
    while cursor <= end:
        window_end = min(cursor + timedelta(days=WINDOW_DAYS - 1), end)
        api_url = _historical_url(api_kind, cursor, window_end)

        # ── Range API ────────────────────────────────────────────────────
        api_blocked_by_html = False
        try:
            raw_rows = fetch_range(session, api_kind, cursor, window_end)
        except HtmlChallengeError as exc:
            logger.warning("%s window %s → %s: %s — skipping archive fallback",
                           dataset_key, cursor, window_end, exc)
            raw_rows = []
            api_blocked_by_html = True
            consecutive_html_blocks += 1
        else:
            consecutive_html_blocks = 0

        records = [parse_api_row(r, scrape_date, api_url) for r in raw_rows]
        records = [r for r in records if r.get("symbol") and r.get("client_name")]

        api_dates = {date.fromisoformat(r["deal_date"]) for r in records if r.get("deal_date")}
        logger.info(
            "%s API window %s → %s: %d rows across %d trading days",
            dataset_key, cursor, window_end, len(records), len(api_dates),
        )

        # ── Archive CSV fallback ─────────────────────────────────────────
        # Skip if NSE served HTML — same auth surface, same block. Bails out
        # of ~270s of futile per-day attempts when bot detection is hot.
        csv_records = []
        if not api_blocked_by_html:
            for d in iter_trading_days(cursor, window_end):
                if d in api_dates:
                    continue
                df, src_url = fetch_archive_csv(session, d, api_kind)
                if df is None:
                    continue
                df.columns = [c.strip().upper() for c in df.columns]
                for _, row in df.iterrows():
                    rec = parse_csv_row(row, d, scrape_date, src_url or "")
                    if rec.get("symbol") and rec.get("client_name"):
                        csv_records.append(rec)

        if csv_records:
            logger.info("%s archive fill: +%d rows", dataset_key, len(csv_records))

        # If we've been blocked for 5 windows in a row, abort — re-warm
        # and retry-on-cron will likely do better than continuing now.
        if consecutive_html_blocks >= 5:
            logger.error(
                "%s: HTML-blocked for 5 consecutive windows — aborting backfill. "
                "Re-run after the next holiday/calendar window or with proxy enabled.",
                dataset_key,
            )
            break

        all_records = records + csv_records
        total_fetched += len(all_records)

        if all_records:
            n = bulk_upsert(table, all_records, conflict_columns=conflict_columns)
            total_upserted += n

        # Advance checkpoint only after the window committed
        set_checkpoint(dataset_key, window_end, rows_added=total_upserted)
        cursor = window_end + timedelta(days=1)

    logger.info("%s backfill done: %d fetched, %d upserted",
                dataset_key, total_fetched, total_upserted)
    return {"fetched": total_fetched, "upserted": total_upserted}
