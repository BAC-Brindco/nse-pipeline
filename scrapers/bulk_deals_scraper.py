"""
Bulk Deals scraper.

A bulk deal is a transaction in which a single client trades >= 0.5% of
the total listed shares of a company on a single exchange in a day.
SEBI made bulk-deal disclosure mandatory effective ~Sept 2009 — earlier
windows will return nothing regardless of source.

Sources (priority order):
  Daily snapshot — used by the daily run only:
      /api/snapshot-capital-market-largedeal
      → BULK_DEALS_DATA key
  Historical range API — used by backfill:
      /api/historical/cm/bulk_deals?from=DD-MM-YYYY&to=DD-MM-YYYY
  Archive CDN CSV — fallback for older years where the API thins out:
      archives.nseindia.com/content/equities/BULK_DEALS_{DD-Mon-YYYY}.csv
"""

import logging
from datetime import date

import pandas as pd

from scrapers.nse_session import NSESession
from scrapers._deals_common import run_historical_backfill
from database.client import bulk_upsert, RunLogger, get_checkpoint
from utils.helpers import (
    clean_str, clean_date, clean_numeric, clean_int, buy_sell_flag, today_ist,
)
from config import BACKFILL_START

logger = logging.getLogger(__name__)

_SNAPSHOT_URL  = "https://www.nseindia.com/api/snapshot-capital-market-largedeal"
_TABLE         = "bulk_deals"
_CONFLICT_COLS = ["deal_date", "symbol", "client_name", "buy_sell", "quantity"]


def _parse_api_row(row: dict, scrape_date: str, source_url: str) -> dict:
    return {
        "deal_date":     clean_date(row.get("date") or row.get("dealDate") or row.get("BD_DT_DATE")),
        "symbol":        clean_str(row.get("symbol") or row.get("BD_SYMBOL")),
        "security_name": clean_str(row.get("secDesc") or row.get("BD_SCRIP_NAME")),
        "client_name":   clean_str(row.get("clientName") or row.get("BD_CLIENT_NAME")),
        "buy_sell":      buy_sell_flag(row.get("buySell") or row.get("BD_BUY_SELL")),
        "quantity":      clean_int(row.get("qty") or row.get("BD_QTY_TRD")),
        "avg_price":     clean_numeric(row.get("avgprice") or row.get("BD_TP_WATP")),
        "exchange":      "NSE",
        "remarks":       clean_str(row.get("remarks") or row.get("BD_REMARKS")),
        "data_source":   "historical_api",
        "source_url":    source_url,
        "raw_payload":   row,
        "scrape_date":   scrape_date,
    }


def _parse_csv_row(row: pd.Series, d: date, scrape_date: str, source_url: str) -> dict:
    return {
        "deal_date":     d.isoformat(),
        "symbol":        clean_str(str(row.get("SYMBOL", ""))),
        "security_name": clean_str(str(row.get("SECURITY NAME", ""))),
        "client_name":   clean_str(str(row.get("CLIENT NAME", ""))),
        "buy_sell":      buy_sell_flag(str(row.get("BUY / SELL", row.get("BUY/SELL", "")))),
        "quantity":      clean_int(str(row.get("QUANTITY TRADED", ""))),
        "avg_price":     clean_numeric(str(
            row.get("TRADE PRICE / WGHT. AVG. PRICE", row.get("TRADE PRICE", ""))
        )),
        "exchange":      "NSE",
        "remarks":       clean_str(str(row.get("REMARKS", ""))),
        "data_source":   "archive_csv",
        "source_url":    source_url,
        "raw_payload":   {k: (None if str(v) == "nan" else v) for k, v in row.to_dict().items()},
        "scrape_date":   scrape_date,
    }


def _parse_snapshot_row(row: dict, scrape_date: str) -> dict:
    return {
        "deal_date":     clean_date(row.get("date")),
        "symbol":        clean_str(row.get("symbol")),
        "security_name": clean_str(row.get("name")),
        "client_name":   clean_str(row.get("clientName")),
        "buy_sell":      buy_sell_flag(row.get("buySell")),
        "quantity":      clean_int(row.get("qty")),
        "avg_price":     clean_numeric(row.get("watp")),
        "exchange":      "NSE",
        "remarks":       clean_str(row.get("remarks")),
        "data_source":   "snapshot",
        "source_url":    _SNAPSHOT_URL,
        "raw_payload":   row,
        "scrape_date":   scrape_date,
    }


def scrape_bulk_deals_daily(session: NSESession | None = None) -> dict:
    session = session or NSESession()
    scrape_date = today_ist()

    with RunLogger("bulk_deals", scrape_date) as run:
        payload = session.get_json(_SNAPSHOT_URL)
        raw_rows = payload.get("BULK_DEALS_DATA", []) if isinstance(payload, dict) else []

        records = [_parse_snapshot_row(r, scrape_date) for r in raw_rows]
        records = [r for r in records if r["symbol"] and r["client_name"]]
        run.set_fetched(len(records))

        n = bulk_upsert(_TABLE, records, conflict_columns=_CONFLICT_COLS)
        run.set_upserted(n)
        logger.info("Bulk deals daily: %d upserted", n)
        return {"fetched": len(records), "upserted": n}


def scrape_bulk_deals_historical(
    session: NSESession | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    resume: bool = True,
) -> dict:
    session = session or NSESession()
    today = today_ist()

    # Resume from checkpoint unless the caller explicitly overrides start.
    if resume and start_date is None:
        ck = get_checkpoint("bulk_deals")
        if ck:
            from datetime import timedelta
            start = ck + timedelta(days=1)
            logger.info("Resuming bulk_deals backfill from checkpoint %s", start)
        else:
            start = date.fromisoformat(BACKFILL_START["bulk_deals"])
    else:
        start = date.fromisoformat(start_date or BACKFILL_START["bulk_deals"])

    end = date.fromisoformat(end_date or today)
    if start > end:
        logger.info("bulk_deals: nothing to backfill (start %s > end %s)", start, end)
        return {"fetched": 0, "upserted": 0}

    return run_historical_backfill(
        dataset_key="bulk_deals",
        api_kind="bulk_deals",
        table=_TABLE,
        conflict_columns=_CONFLICT_COLS,
        parse_api_row=_parse_api_row,
        parse_csv_row=_parse_csv_row,
        session=session,
        start=start,
        end=end,
    )
