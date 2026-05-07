"""
Wayback Machine scraper — reconstructs ASM/GSM history that NSE's
snapshot endpoint doesn't expose.

NSE's `/api/reportASM` and `/api/reportGSM` only return current state.
But web.archive.org has been periodically caching these endpoints for
years. We pull the archived snapshots, parse each one, and persist into
asm_history_wayback / gsm_history_wayback keyed by the snapshot date.

This gives a quant desk a true point-in-time view of who was on ASM/GSM
on date X, going back as far as the Wayback coverage allows (typically
1-3 years for high-traffic NSE pages).

Pipeline:
  1. CDX search → list of (timestamp, original_url) for the endpoint
  2. Sample at most 1 snapshot per `min_gap_days` to bound cost
  3. Fetch each via /web/{ts}id_/{url} (id_ flag = raw content, no
     archive.org HTML wrapper)
  4. Parse with the same logic as live scrapers
  5. Upsert with snapshot_date as part of the unique key

Wayback's CDX API and `id_` fetches are unauthenticated and not bot-
detected — plain `requests` is sufficient.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Iterable

import requests

from database.client import bulk_upsert, RunLogger
from utils.helpers import clean_str, today_ist

logger = logging.getLogger(__name__)

_CDX_URL = "https://web.archive.org/cdx/search/cdx"
_WAYBACK_REPLAY = "https://web.archive.org/web/{ts}id_/{url}"

# Roman numeral map shared with gsm_scraper
_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6}


def _cdx_search(target_url: str, *, from_date: str, to_date: str) -> list[dict]:
    """Returns list of {timestamp, original} for successful captures."""
    params = {
        "url":      target_url,
        "output":   "json",
        "from":     from_date.replace("-", ""),
        "to":       to_date.replace("-", ""),
        "filter":   "statuscode:200",
        "fl":       "timestamp,original,length",
        "collapse": "timestamp:8",  # collapse to one per day max
    }
    resp = requests.get(_CDX_URL, params=params, timeout=30)
    resp.raise_for_status()
    rows = resp.json()
    if not rows or len(rows) < 2:
        return []
    headers = rows[0]
    return [dict(zip(headers, r)) for r in rows[1:]]


def _fetch_snapshot(timestamp: str, original_url: str) -> dict | None:
    """Fetch the raw JSON content of one Wayback capture."""
    url = _WAYBACK_REPLAY.format(ts=timestamp, url=original_url)
    try:
        resp = requests.get(url, timeout=45)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Wayback fetch failed (%s, %s): %s", timestamp, original_url, exc)
        return None


def _sample_snapshots(snapshots: list[dict], min_gap_days: int) -> Iterable[dict]:
    """Keep at most one snapshot per `min_gap_days`-day bucket."""
    last_kept: date | None = None
    for snap in sorted(snapshots, key=lambda s: s["timestamp"]):
        ts = snap["timestamp"]
        snap_date = datetime.strptime(ts[:8], "%Y%m%d").date()
        if last_kept is None or (snap_date - last_kept).days >= min_gap_days:
            yield snap
            last_kept = snap_date


# ─────────────────────────────────────────────────────────────────────────────
# ASM
# ─────────────────────────────────────────────────────────────────────────────
def _parse_asm_snapshot(payload: dict, snap_date: date, wayback_url: str,
                        scrape_date: str) -> list[dict]:
    out: list[dict] = []
    for json_key, asm_type in (("shortterm", "short_term"), ("longterm", "long_term")):
        section = payload.get(json_key, {})
        rows = section.get("data", []) if isinstance(section, dict) else []
        for row in rows:
            sym = clean_str(row.get("symbol"))
            if not sym:
                continue
            out.append({
                "snapshot_date": snap_date.isoformat(),
                "wayback_url":   wayback_url,
                "symbol":        sym,
                "series":        clean_str(row.get("series")) or "EQ",
                "company_name":  clean_str(row.get("companyName")),
                "isin":          clean_str(row.get("isin")),
                "asm_type":      asm_type,
                "stage":         clean_str(row.get("asmSurvIndicator")),
                "raw_payload":   row,
                "scrape_date":   scrape_date,
            })
    return out


def scrape_asm_history_from_wayback(
    *, from_date: str = "2025-01-01",
    to_date: str | None = None,
    min_gap_days: int = 7,
) -> dict:
    """Backfill historical ASM lists from Wayback. Default: weekly snapshots from 2025."""
    scrape_date = today_ist()
    to_date = to_date or scrape_date
    target = "https://www.nseindia.com/api/reportASM"

    with RunLogger("asm_wayback", scrape_date) as run:
        snapshots = _cdx_search(target, from_date=from_date, to_date=to_date)
        logger.info("Wayback CDX: %d ASM snapshots in [%s..%s]",
                    len(snapshots), from_date, to_date)

        kept = list(_sample_snapshots(snapshots, min_gap_days))
        logger.info("Sampling 1 per %d days → %d snapshots to fetch", min_gap_days, len(kept))

        total = 0
        for i, snap in enumerate(kept, 1):
            ts = snap["timestamp"]
            snap_date = datetime.strptime(ts[:8], "%Y%m%d").date()
            wb_url = _WAYBACK_REPLAY.format(ts=ts, url=snap["original"])
            logger.info("ASM wayback %d/%d: %s", i, len(kept), snap_date)

            payload = _fetch_snapshot(ts, snap["original"])
            if not payload:
                continue

            records = _parse_asm_snapshot(payload, snap_date, wb_url, scrape_date)
            if records:
                n = bulk_upsert(
                    "asm_history_wayback", records,
                    conflict_columns=["snapshot_date", "symbol", "asm_type", "stage"],
                )
                total += n
            time.sleep(1.0)  # polite to archive.org

        run.set_fetched(len(kept))
        run.set_upserted(total)
        logger.info("ASM wayback done: %d snapshots, %d rows upserted", len(kept), total)
        return {"snapshots": len(kept), "upserted": total}


# ─────────────────────────────────────────────────────────────────────────────
# GSM
# ─────────────────────────────────────────────────────────────────────────────
def _parse_gsm_stage(row: dict) -> int | None:
    raw = row.get("gsmStage")
    if raw is not None:
        s = str(raw).strip().upper()
        if s.isdigit():
            return int(s)
        if s in _ROMAN:
            return _ROMAN[s]
    return None


def _parse_gsm_snapshot(payload: dict, snap_date: date, wayback_url: str,
                        scrape_date: str) -> list[dict]:
    rows = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        sym = clean_str(row.get("symbol"))
        if not sym:
            continue
        out.append({
            "snapshot_date": snap_date.isoformat(),
            "wayback_url":   wayback_url,
            "symbol":        sym,
            "series":        "EQ",
            "company_name":  clean_str(row.get("companyName")),
            "isin":          clean_str(row.get("isin")),
            "stage":         _parse_gsm_stage(row),
            "raw_payload":   row,
            "scrape_date":   scrape_date,
        })
    return out


def scrape_gsm_history_from_wayback(
    *, from_date: str = "2025-01-01",
    to_date: str | None = None,
    min_gap_days: int = 7,
) -> dict:
    scrape_date = today_ist()
    to_date = to_date or scrape_date
    target = "https://www.nseindia.com/api/reportGSM"

    with RunLogger("gsm_wayback", scrape_date) as run:
        snapshots = _cdx_search(target, from_date=from_date, to_date=to_date)
        logger.info("Wayback CDX: %d GSM snapshots in [%s..%s]",
                    len(snapshots), from_date, to_date)

        kept = list(_sample_snapshots(snapshots, min_gap_days))
        logger.info("Sampling 1 per %d days → %d snapshots to fetch", min_gap_days, len(kept))

        total = 0
        for i, snap in enumerate(kept, 1):
            ts = snap["timestamp"]
            snap_date = datetime.strptime(ts[:8], "%Y%m%d").date()
            wb_url = _WAYBACK_REPLAY.format(ts=ts, url=snap["original"])
            logger.info("GSM wayback %d/%d: %s", i, len(kept), snap_date)

            payload = _fetch_snapshot(ts, snap["original"])
            if not payload:
                continue

            records = _parse_gsm_snapshot(payload, snap_date, wb_url, scrape_date)
            # Drop rows with NULL stage from the dedup-keyed upsert
            with_stage = [r for r in records if r["stage"] is not None]
            if with_stage:
                n = bulk_upsert(
                    "gsm_history_wayback", with_stage,
                    conflict_columns=["snapshot_date", "symbol", "stage"],
                )
                total += n
            time.sleep(1.0)

        run.set_fetched(len(kept))
        run.set_upserted(total)
        logger.info("GSM wayback done: %d snapshots, %d rows upserted", len(kept), total)
        return {"snapshots": len(kept), "upserted": total}


def scrape_wayback_history(**kwargs) -> dict:
    """Convenience entrypoint — runs both ASM and GSM Wayback scrapes."""
    asm = scrape_asm_history_from_wayback(**kwargs)
    gsm = scrape_gsm_history_from_wayback(**kwargs)
    return {"asm": asm, "gsm": gsm}
