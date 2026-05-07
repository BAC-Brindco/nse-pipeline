"""
T2T (Trade-to-Trade / BE series) scraper.

Securities in the T2T segment must be compulsorily settled on a
gross basis (delivery only) — intraday squaring-off is not permitted.

Primary source:
  https://www.nseindia.com/api/trade-info?index=T2T
  (falls back to: https://www.nseindia.com/api/reportsmf?index=T2Tsecurities)

NSE also publishes a downloadable circular-based list; we try both endpoints
and take whichever responds with a valid list.
"""

import logging

from scrapers.nse_session import NSESession
from database.client import bulk_upsert, RunLogger
from utils.helpers import clean_str, clean_date, today_ist

logger = logging.getLogger(__name__)

_T2T_ENDPOINTS = [
    "https://www.nseindia.com/api/trade-info?index=T2T",
    "https://www.nseindia.com/api/reportsmf?index=T2Tsecurities",
]


def _parse_t2t_record(row: dict, scrape_date: str) -> dict:
    return {
        "symbol":           clean_str(row.get("symbol") or row.get("Symbol") or row.get("SYMBOL")),
        "series":           clean_str(row.get("series") or row.get("Series") or "BE"),
        "company_name":     clean_str(row.get("secDesc") or row.get("companyName") or row.get("NAME OF SECURITY")),
        "isin":             clean_str(row.get("isin") or row.get("ISIN")),
        "date_of_addition": clean_date(row.get("addDate") or row.get("dateOfAddition") or row.get("DATE")),
        "date_of_removal":  clean_date(row.get("removeDate") or row.get("dateOfRemoval")),
        "remarks":          clean_str(row.get("remarks") or row.get("Remarks") or row.get("REASON")),
        "scrape_date":      scrape_date,
    }


def scrape_t2t(session: NSESession | None = None) -> dict:
    session = session or NSESession()
    scrape_date = today_ist()

    raw_rows: list | None = None
    for url in _T2T_ENDPOINTS:
        try:
            payload = session.get_json(url)
            candidate = payload.get("data", payload) if isinstance(payload, dict) else payload
            if isinstance(candidate, list) and len(candidate) > 0:
                raw_rows = candidate
                logger.info("T2T data from %s (%d rows)", url, len(raw_rows))
                break
        except Exception as exc:  # noqa: BLE001
            logger.warning("T2T endpoint %s failed: %s", url, exc)

    if raw_rows is None:
        logger.error("All T2T endpoints failed")
        return {"fetched": 0, "upserted": 0}

    with RunLogger("t2t", scrape_date) as run:
        records = [_parse_t2t_record(r, scrape_date) for r in raw_rows]
        records = [r for r in records if r["symbol"]]
        run.set_fetched(len(records))

        n = bulk_upsert(
            "t2t_list",
            records,
            conflict_columns=["symbol", "series", "date_of_addition"],
        )
        run.set_upserted(n)
        logger.info("T2T: %d records upserted", n)
        return {"fetched": len(records), "upserted": n}
