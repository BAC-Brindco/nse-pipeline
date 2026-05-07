"""
Short Selling scraper.

NSE publishes daily short position reports (SHORT_DEALS_DATA) via the
large-deal snapshot endpoint. Records show symbol and quantity shorted
but do NOT include client name, price, or buy/sell direction.

API: https://www.nseindia.com/api/snapshot-capital-market-largedeal
"""

import logging

from scrapers.nse_session import NSESession
from database.client import bulk_upsert, RunLogger
from utils.helpers import clean_str, clean_date, clean_int, today_ist

logger = logging.getLogger(__name__)

_SNAPSHOT_URL = "https://www.nseindia.com/api/snapshot-capital-market-largedeal"


def _parse_record(row: dict, scrape_date: str) -> dict:
    return {
        "deal_date":     clean_date(row.get("date")),
        "symbol":        clean_str(row.get("symbol")),
        "security_name": clean_str(row.get("name")),
        "quantity":      clean_int(row.get("qty")),
        "exchange":      "NSE",
        "scrape_date":   scrape_date,
    }


def scrape_short_deals_daily(session: NSESession | None = None) -> dict:
    session = session or NSESession()
    scrape_date = today_ist()

    with RunLogger("short_deals", scrape_date) as run:
        payload = session.get_json(_SNAPSHOT_URL)
        raw_rows = payload.get("SHORT_DEALS_DATA", []) if isinstance(payload, dict) else []

        records = [_parse_record(r, scrape_date) for r in raw_rows]
        records = [r for r in records if r["symbol"]]
        run.set_fetched(len(records))

        n = bulk_upsert(
            "short_deals", records,
            conflict_columns=["deal_date", "symbol"],
        )
        run.set_upserted(n)
        logger.info("Short deals daily: %d upserted", n)
        return {"fetched": len(records), "upserted": n}
