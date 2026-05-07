"""
NSE trading-holiday scraper.

Source:
  https://www.nseindia.com/api/holiday-master?type=trading

Persists every published holiday to `nse_trading_holidays`. The helpers
in `utils.helpers` (`is_trading_day`, `iter_trading_days`) consult this
table once at process startup and cache in memory.

Run on the daily workflow — holiday lists change ~yearly when NSE
publishes the next year's calendar in late December.
"""

import logging
from typing import Iterable

from scrapers.nse_session import NSESession
from database.client import bulk_upsert, RunLogger
from utils.helpers import clean_str, clean_date, today_ist

logger = logging.getLogger(__name__)

_HOLIDAYS_URL = "https://www.nseindia.com/api/holiday-master?type=trading"


def _records_from_payload(payload: dict, scrape_date: str) -> Iterable[dict]:
    # Payload shape:
    # { "CM": [ {tradingDate, weekDay, description, ...}, ... ],
    #   "FO": [...],  "CD": [...],  "SLBS": [...] }
    if not isinstance(payload, dict):
        return []

    out: list[dict] = []
    for segment, rows in payload.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            holiday_date = clean_date(row.get("tradingDate"))
            if not holiday_date:
                continue
            out.append({
                "holiday_date":  holiday_date,
                "segment":       clean_str(segment),
                "description":   clean_str(row.get("description")),
                "weekday":       clean_str(row.get("weekDay")),
                "scrape_date":   scrape_date,
            })
    return out


def scrape_holidays(session: NSESession | None = None) -> dict:
    session = session or NSESession()
    scrape_date = today_ist()

    with RunLogger("holidays", scrape_date) as run:
        payload = session.get_json(_HOLIDAYS_URL)
        records = list(_records_from_payload(payload, scrape_date))
        run.set_fetched(len(records))

        if not records:
            logger.warning("Holiday scrape: no rows parsed from payload")
            return {"fetched": 0, "upserted": 0}

        n = bulk_upsert(
            "nse_trading_holidays",
            records,
            conflict_columns=["holiday_date", "segment"],
        )
        run.set_upserted(n)
        logger.info("Holidays: %d records upserted", n)
        return {"fetched": len(records), "upserted": n}
