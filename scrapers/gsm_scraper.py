"""
GSM (Graded Surveillance Measure) scraper.

API: https://www.nseindia.com/api/reportGSM

Response shape: { "data": [ {...}, ... ] }  (or a bare list)

Each record: symbol, companyName, isin, gsmStage (Roman numeral),
gsmTime (DD-Mon-YYYY HH:MM:SS), survCode (contains numeric stage in
parentheses e.g. "GSM - VI (6)" or "IBC - Receipt & GSM 0 (62)"),
survDesc, srno.
"""

import re
import logging

from scrapers.nse_session import NSESession
from database.client import bulk_upsert, RunLogger
from utils.helpers import clean_str, clean_date, today_ist

logger = logging.getLogger(__name__)

_GSM_URL = "https://www.nseindia.com/api/reportGSM"
_STAGE_RE = re.compile(r'\((\d+)\)')


def _parse_stage(surv_code: str | None, gsm_stage: str | None) -> int | None:
    """Extract numeric stage from survCode e.g. 'GSM - VI (6)' → 6."""
    if surv_code:
        m = _STAGE_RE.search(surv_code)
        if m:
            return int(m.group(1))
    # fallback: basic Roman numeral map for standard stages
    if gsm_stage:
        roman = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6}
        return roman.get(str(gsm_stage).strip().upper())
    return None


def _parse_gsm_date(val: str | None) -> str | None:
    """gsmTime arrives as 'DD-Mon-YYYY HH:MM:SS'; strip time before parsing."""
    if not val:
        return None
    return clean_date(str(val).split()[0])


def _parse_record(row: dict, scrape_date: str) -> dict:
    return {
        "symbol":           clean_str(row.get("symbol")),
        "series":           "EQ",
        "company_name":     clean_str(row.get("companyName")),
        "isin":             clean_str(row.get("isin")),
        "stage":            _parse_stage(row.get("survCode"), row.get("gsmStage")),
        "date_of_addition": _parse_gsm_date(row.get("gsmTime")),
        "date_of_removal":  None,
        "remarks":          clean_str(row.get("survDesc")),
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
        records = [r for r in records if r["symbol"] and r["stage"] is not None]
        run.set_fetched(len(records))

        n = bulk_upsert(
            "gsm_list",
            records,
            conflict_columns=["symbol", "series", "stage", "date_of_addition"],
        )
        run.set_upserted(n)
        logger.info("GSM: %d records upserted", n)
        return {"fetched": len(records), "upserted": n}
