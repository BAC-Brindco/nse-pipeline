"""
ASM (Additional Surveillance Measure) scraper.

NSE maintains short-term and long-term ASM lists, served together
from one endpoint:
  https://www.nseindia.com/api/reportASM

Response shape:
  {
    "shortterm": { "data": [ {...}, ... ] },
    "longterm":  { "data": [ {...}, ... ] }
  }

IMPORTANT — addition-date is unreliable:
NSE's snapshot does NOT expose the actual date a stock entered ASM.
The `asmTime` field is a refresh timestamp, not an entry date.
We therefore:
  1. Store asmTime in raw_payload (JSONB) and date_of_addition gets
     parsed only as a best-effort.
  2. Rely on the BEFORE INSERT/UPDATE trigger `maintain_seen_dates`
     to track first_seen_date / last_seen_date from daily snapshots —
     that gives us TRUE entry/exit windows accumulating forward.
  3. For history older than first daily snapshot, see
     wayback_scraper.py which reconstructs from web.archive.org.
"""

import logging

from scrapers.nse_session import NSESession
from database.client import bulk_upsert, RunLogger
from utils.helpers import clean_str, clean_date, today_ist

logger = logging.getLogger(__name__)

_ASM_URL = "https://www.nseindia.com/api/reportASM"

_LIST_KEYS = {
    "shortterm": "short_term",
    "longterm":  "long_term",
}


def _parse_record(row: dict, asm_type: str, scrape_date: str) -> dict:
    return {
        "symbol":           clean_str(row.get("symbol")),
        "series":           clean_str(row.get("series")) or "EQ",
        "company_name":     clean_str(row.get("companyName")),
        "isin":             clean_str(row.get("isin")),
        "asm_type":         asm_type,
        "stage":            clean_str(row.get("asmSurvIndicator")),
        # asmTime is unreliable; record only as best-effort. The trigger on
        # this table maintains first_seen_date / last_seen_date as the
        # source of truth for when this stock was on this ASM stage.
        "date_of_addition": clean_date(row.get("asmTime")),
        "date_of_removal":  None,
        "reason":           clean_str(row.get("survCode")),
        "remarks":          clean_str(row.get("survDesc")),
        "data_source":      "snapshot",
        "source_url":       _ASM_URL,
        "raw_payload":      row,
        "scrape_date":      scrape_date,
        # first_seen_date / last_seen_date set automatically by trigger
    }


def scrape_asm(session: NSESession | None = None) -> dict:
    session = session or NSESession()
    scrape_date = today_ist()
    totals = {"fetched": 0, "upserted": 0}

    with RunLogger("asm", scrape_date) as run:
        payload = session.get_json(_ASM_URL)

        for json_key, asm_type in _LIST_KEYS.items():
            section = payload.get(json_key, {})
            raw_rows = section.get("data", []) if isinstance(section, dict) else []

            if not raw_rows:
                logger.warning("ASM %s: no data in response", asm_type)
                continue

            records = [_parse_record(r, asm_type, scrape_date) for r in raw_rows]
            records = [r for r in records if r["symbol"]]
            totals["fetched"] += len(records)

            n = bulk_upsert(
                "asm_list",
                records,
                conflict_columns=["symbol", "series", "asm_type", "stage"],
            )
            totals["upserted"] += n
            logger.info("ASM %s: %d records upserted", asm_type, n)

        run.set_fetched(totals["fetched"])
        run.set_upserted(totals["upserted"])

    return totals
