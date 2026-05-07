"""
ASM (Additional Surveillance Measure) scraper.

NSE maintains two ASM lists (short-term and long-term) served together
from a single endpoint:
  https://www.nseindia.com/api/reportASM

Response shape:
  {
    "shortterm": { "data": [ {...}, ... ] },
    "longterm":  { "data": [ {...}, ... ] }
  }

Each record contains: symbol, series, companyName, isin,
asmSurvIndicator (stage), asmTime (date added), survCode, survDesc.
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
        "date_of_addition": clean_date(row.get("asmTime")),
        "date_of_removal":  None,
        "reason":           clean_str(row.get("survCode")),
        "remarks":          clean_str(row.get("survDesc")),
        "data_source":      "snapshot",
        "source_url":       _ASM_URL,
        "scrape_date":      scrape_date,
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
                conflict_columns=["symbol", "series", "asm_type", "date_of_addition"],
            )
            totals["upserted"] += n
            logger.info("ASM %s: %d records upserted", asm_type, n)

        run.set_fetched(totals["fetched"])
        run.set_upserted(totals["upserted"])

    return totals
