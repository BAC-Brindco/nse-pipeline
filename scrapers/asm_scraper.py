"""
ASM (Additional Surveillance Measure) scraper.

NSE maintains two ASM lists:
  - Short-term ASM  (triggered by short-term price/volume criteria)
  - Long-term ASM   (sustained surveillance)

API endpoints (session-authenticated):
  https://www.nseindia.com/api/reportsmf?index=shortTermASM
  https://www.nseindia.com/api/reportsmf?index=longTermASM

Each response contains a 'data' array with current-list entries.
Historical snapshots aren't directly available via API; we preserve
each daily scrape as a unique row keyed on (symbol, asm_type, scrape_date).
"""

import logging
from datetime import date

from scrapers.nse_session import NSESession
from database.client import bulk_upsert, RunLogger
from utils.helpers import clean_str, clean_date, today_ist

logger = logging.getLogger(__name__)

_ASM_ENDPOINTS = {
    "short_term": "https://www.nseindia.com/api/reportsmf?index=shortTermASM",
    "long_term":  "https://www.nseindia.com/api/reportsmf?index=longTermASM",
}


def _parse_asm_record(row: dict, asm_type: str, scrape_date: str) -> dict:
    return {
        "symbol":           clean_str(row.get("symbol") or row.get("Symbol") or row.get("SYMBOL")),
        "series":           clean_str(row.get("series") or row.get("Series") or "EQ"),
        "company_name":     clean_str(row.get("secDesc") or row.get("companyName") or row.get("NAME OF SECURITY")),
        "isin":             clean_str(row.get("isin") or row.get("ISIN")),
        "asm_type":         asm_type,
        "stage":            clean_str(row.get("stage") or row.get("Stage") or row.get("asmIdentifier")),
        "date_of_addition": clean_date(row.get("addDate") or row.get("dateOfAddition") or row.get("DATE OF INCLUSION")),
        "date_of_removal":  clean_date(row.get("removeDate") or row.get("dateOfRemoval")),
        "reason":           clean_str(row.get("reason") or row.get("Reason")),
        "remarks":          clean_str(row.get("remarks") or row.get("Remarks")),
        "scrape_date":      scrape_date,
    }


def scrape_asm(session: NSESession | None = None) -> dict:
    session = session or NSESession()
    scrape_date = today_ist()
    totals = {"fetched": 0, "upserted": 0}

    for asm_type, url in _ASM_ENDPOINTS.items():
        with RunLogger("asm_" + asm_type, scrape_date) as run:
            try:
                payload = session.get_json(url)
                raw_rows = payload.get("data", payload) if isinstance(payload, dict) else payload
                if not isinstance(raw_rows, list):
                    logger.warning("Unexpected ASM payload shape for %s", asm_type)
                    continue

                records = [_parse_asm_record(r, asm_type, scrape_date) for r in raw_rows]
                records = [r for r in records if r["symbol"]]
                run.set_fetched(len(records))
                totals["fetched"] += len(records)

                n = bulk_upsert(
                    "asm_list",
                    records,
                    conflict_columns=["symbol", "series", "asm_type", "date_of_addition"],
                )
                run.set_upserted(n)
                totals["upserted"] += n
                logger.info("ASM %s: %d records upserted", asm_type, n)

            except Exception as exc:
                run.fail(str(exc))
                logger.exception("ASM %s scrape failed: %s", asm_type, exc)

    return totals
