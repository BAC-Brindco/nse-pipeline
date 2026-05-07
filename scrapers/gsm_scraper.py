"""
GSM (Graded Surveillance Measure) scraper.

API: https://www.nseindia.com/api/reportGSM

Response shape: { "data": [ {...}, ... ] }

Each record carries: symbol, companyName, isin, gsmStage (Roman numeral
or "0"), gsmTime (DD-Mon-YYYY HH:MM:SS), survCode, survDesc, srno.

Stage parsing is non-trivial — survCode mixes presentations:
  "GSM - VI (6)"                    → stage 6
  "GSM - 0 (50)"                    → stage 0  (NOT 50; 50 is NSE's internal code)
  "IBC - Receipt & GSM 0 (62)"      → stage 0  (62 is internal)
  "LTASM Stage I and GSM Stage 0"   → stage 0
We extract from gsmStage / gsmStageDescription first; survDesc /
survCode is fallback. Internal codes in trailing parens are ignored.

Same caveat as ASM about historical addition dates: gsmTime is a refresh
timestamp, not an entry date — see asm_scraper.py docstring. We rely on
the maintain_seen_dates trigger for first_seen / last_seen tracking.
"""

import re
import logging

from scrapers.nse_session import NSESession
from database.client import bulk_upsert, RunLogger
from utils.helpers import clean_str, clean_date, today_ist

logger = logging.getLogger(__name__)

_GSM_URL = "https://www.nseindia.com/api/reportGSM"

# Roman numeral → int for GSM stages I-VI
_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6}


def _parse_stage(row: dict) -> int | None:
    """
    Find the GSM stage in the row, ignoring NSE-internal numeric codes
    that appear in trailing parentheses (e.g. "(50)" or "(62)").
    """
    # Best signal: gsmStage field directly
    raw = row.get("gsmStage")
    if raw is not None:
        s = str(raw).strip().upper()
        if s.isdigit():
            return int(s)
        if s in _ROMAN:
            return _ROMAN[s]

    # Next: gsmStageDescription / survDesc — match "Stage I" / "Stage 0" / "Stage VI"
    for field in ("gsmStageDescription", "survDesc", "survCode"):
        text = row.get(field)
        if not text:
            continue
        # Strip trailing parenthetical (NSE internal codes like "(50)")
        s = re.sub(r"\([^)]*\)\s*$", "", str(text)).strip()
        m = re.search(r"Stage\s+(0|[IVX]+|\d+)", s, re.IGNORECASE)
        if not m:
            # Fallback: leading "GSM - VI" pattern
            m = re.search(r"GSM\s*-\s*(0|[IVX]+|\d+)", s, re.IGNORECASE)
        if m:
            token = m.group(1).upper()
            if token.isdigit():
                return int(token)
            if token in _ROMAN:
                return _ROMAN[token]
    return None


def _parse_gsm_date(val: str | None) -> str | None:
    """gsmTime arrives as 'DD-Mon-YYYY HH:MM:SS' — clean_date now strips the time component."""
    if not val:
        return None
    return clean_date(val)


def _parse_record(row: dict, scrape_date: str) -> dict:
    return {
        "symbol":           clean_str(row.get("symbol")),
        "series":           "EQ",
        "company_name":     clean_str(row.get("companyName")),
        "isin":             clean_str(row.get("isin")),
        "stage":            _parse_stage(row),
        "date_of_addition": _parse_gsm_date(row.get("gsmTime")),
        "date_of_removal":  None,
        "remarks":          clean_str(row.get("survDesc")),
        "data_source":      "snapshot",
        "source_url":       _GSM_URL,
        "raw_payload":      row,
        "scrape_date":      scrape_date,
    }


def scrape_gsm(session: NSESession | None = None) -> dict:
    session = session or NSESession()
    scrape_date = today_ist()

    with RunLogger("gsm", scrape_date) as run:
        payload = session.get_json(_GSM_URL)

        raw_rows = (
            payload.get("data", payload) if isinstance(payload, dict) else payload
        )
        if not isinstance(raw_rows, list):
            logger.warning("Unexpected GSM payload shape")
            run.fail("Unexpected payload shape")
            return {"fetched": 0, "upserted": 0}

        records = [_parse_record(r, scrape_date) for r in raw_rows]
        # Reject rows missing symbol; allow stage=None — raw_payload still preserved
        records = [r for r in records if r["symbol"]]
        run.set_fetched(len(records))

        # Filter to rows we can dedup. Rows without a stage land via a
        # second pass that uses INSERT (no upsert) so we don't lose them.
        with_stage    = [r for r in records if r["stage"] is not None]
        without_stage = [r for r in records if r["stage"] is None]

        n = bulk_upsert(
            "gsm_list",
            with_stage,
            conflict_columns=["symbol", "series", "stage"],
        )
        if without_stage:
            logger.warning(
                "GSM: %d rows had unparseable stage (preserved in raw_payload only)",
                len(without_stage),
            )
            # Best-effort insert without dedup; safe because stage NULL won't
            # collide with the unique key. raw_payload preserves the truth.
            bulk_upsert("gsm_list", without_stage, conflict_columns=["symbol", "series", "stage"])

        run.set_upserted(n)
        logger.info("GSM: %d records upserted", n)
        return {"fetched": len(records), "upserted": n}
