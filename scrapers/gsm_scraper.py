"""
GSM (Graded Surveillance Measure) scraper.

NSE classifies securities into 6 GSM stages based on price-to-earnings,
price-to-book, and other fundamental criteria.

API: https://www.nseindia.com/api/reportsmf?index=GSMsecurities

The JSON response contains a 'data' array; each row has a 'stage' field
indicating the GSM stage (I–VI).
"""

import logging

from scrapers.nse_session import NSESession
from database.client import bulk_upsert, RunLogger
from utils.helpers import clean_str, clean_date, clean_int, today_ist

logger = logging.getLogger(__name__)

_GSM_URL = "https://www.nseindia.com/api/reportsmf?index=GSMsecurities"

_STAGE_MAP = {
    "I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6,
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6,
}


def _parse_stage(val) -> int | None:
    if val is None:
        return None
    key = str(val).strip().upper()
    return _STAGE_MAP.get(key) or clean_int(val)


def _parse_gsm_record(row: dict, scrape_date: str) -> dict:
    return {
        "symbol":           clean_str(row.get("symbol") or row.get("Symbol") or row.get("SYMBOL")),
        "series":           clean_str(row.get("series") or row.get("Series") or "EQ"),
        "company_name":     clean_str(row.get("secDesc") or row.get("companyName") or row.get("NAME OF SECURITY")),
        "isin":             clean_str(row.get("isin") or row.get("ISIN")),
        "stage":            _parse_stage(row.get("stage") or row.get("Stage") or row.get("gsmStage")),
        "date_of_addition": clean_date(row.get("addDate") or row.get("dateOfAddition") or row.get("DATE OF INCLUSION")),
        "date_of_removal":  clean_date(row.get("removeDate") or row.get("dateOfRemoval")),
        "remarks":          clean_str(row.get("remarks") or row.get("Remarks")),
        "scrape_date":      scrape_date,
    }


def scrape_gsm(session: NSESession | None = None) -> dict:
    session = session or NSESession()
    scrape_date = today_ist()

    with RunLogger("gsm", scrape_date) as run:
        payload = session.get_json(_GSM_URL)
        raw_rows = payload.get("data", payload) if isinstance(payload, dict) else payload

        if not isinstance(raw_rows, list):
            logger.warning("Unexpected GSM payload shape")
            run.fail("Unexpected payload shape")
            return {"fetched": 0, "upserted": 0}

        records = [_parse_gsm_record(r, scrape_date) for r in raw_rows]
        records = [r for r in records if r["symbol"]]
        run.set_fetched(len(records))

        n = bulk_upsert(
            "gsm_list",
            records,
            conflict_columns=["symbol", "series", "stage", "date_of_addition"],
        )
        run.set_upserted(n)
        logger.info("GSM: %d records upserted", n)
        return {"fetched": len(records), "upserted": n}
