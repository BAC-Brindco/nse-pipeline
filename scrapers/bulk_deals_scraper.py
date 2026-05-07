"""
Bulk Deals scraper.

A bulk deal occurs when a single client trades >= 0.5% of the total listed
shares of a company in a single day on any exchange.

Sources:
  Daily API:  https://www.nseindia.com/api/bulk-deal-advance
              (params: from_date, to_date in DD-MM-YYYY format)

  Historical archives (NSE CDN):
      https://archives.nseindia.com/content/equities/BULK_DEALS_{DD-Mon-YYYY}.csv
      https://archives.nseindia.com/content/equities/bulk_deals_{DDMMYYYY}.csv

  We try the archive first for backfill; fall back to API for recent dates.
"""

import io
import logging
from datetime import date, timedelta

import pandas as pd
import requests

from scrapers.nse_session import NSESession
from database.client import bulk_upsert, RunLogger
from utils.helpers import (
    clean_str, clean_date, clean_numeric, clean_int,
    buy_sell_flag, today_ist, date_range,
)
from config import BACKFILL_START, NSE_ARCHIVE_URL, REQUEST_DELAY_SECONDS

logger = logging.getLogger(__name__)

_BULK_API_URL = "https://www.nseindia.com/api/bulk-deal-advance"


def _archive_urls(d: date) -> list[str]:
    mon3 = d.strftime("%b").capitalize()  # Jan, Feb, …
    ddmmyyyy = d.strftime("%d%m%Y")
    ddmonyyyy = d.strftime("%d-") + mon3 + d.strftime("-%Y")  # 01-Jan-2023
    return [
        f"{NSE_ARCHIVE_URL}/content/equities/BULK_DEALS_{ddmonyyyy}.csv",
        f"{NSE_ARCHIVE_URL}/content/equities/bulk_deals_{ddmmyyyy}.csv",
        f"{NSE_ARCHIVE_URL}/content/equities/BULK_DEALS_{ddmmyyyy}.csv",
    ]


def _parse_api_row(row: dict, scrape_date: str) -> dict:
    return {
        "deal_date":     clean_date(row.get("date") or row.get("dealDate")),
        "symbol":        clean_str(row.get("symbol") or row.get("Symbol")),
        "security_name": clean_str(row.get("secDesc") or row.get("securityName")),
        "client_name":   clean_str(row.get("clientName") or row.get("clientname")),
        "buy_sell":      buy_sell_flag(row.get("buySell") or row.get("transactionType")),
        "quantity":      clean_int(row.get("qty") or row.get("quantity")),
        "avg_price":     clean_numeric(row.get("avgprice") or row.get("price") or row.get("tradePrice")),
        "exchange":      "NSE",
        "remarks":       clean_str(row.get("remarks")),
        "scrape_date":   scrape_date,
    }


def _parse_csv_df(df: pd.DataFrame, d: date, scrape_date: str) -> list[dict]:
    df.columns = [c.strip().upper() for c in df.columns]
    col_map = {
        "SYMBOL": "symbol",
        "SECURITY NAME": "security_name",
        "CLIENT NAME": "client_name",
        "BUY / SELL": "buy_sell",
        "QUANTITY TRADED": "quantity",
        "TRADE PRICE / WGHT. AVG. PRICE": "avg_price",
        "REMARKS": "remarks",
        "DATE": "deal_date",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

    records = []
    for _, row in df.iterrows():
        records.append({
            "deal_date":     d.isoformat(),
            "symbol":        clean_str(str(row.get("symbol", ""))),
            "security_name": clean_str(str(row.get("security_name", ""))),
            "client_name":   clean_str(str(row.get("client_name", ""))),
            "buy_sell":      buy_sell_flag(str(row.get("buy_sell", ""))),
            "quantity":      clean_int(str(row.get("quantity", ""))),
            "avg_price":     clean_numeric(str(row.get("avg_price", ""))),
            "exchange":      "NSE",
            "remarks":       clean_str(str(row.get("remarks", ""))),
            "scrape_date":   scrape_date,
        })
    return records


def _fetch_archive(session: NSESession, d: date, scrape_date: str) -> list[dict] | None:
    for url in _archive_urls(d):
        try:
            resp = session.get(url)
            df = pd.read_csv(io.StringIO(resp.text))
            if df.empty:
                continue
            return _parse_csv_df(df, d, scrape_date)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Archive %s failed: %s", url, exc)
    return None


def _fetch_api(session: NSESession, from_dt: date, to_dt: date, scrape_date: str) -> list[dict]:
    params = {
        "from_date": from_dt.strftime("%d-%m-%Y"),
        "to_date":   to_dt.strftime("%d-%m-%Y"),
    }
    payload = session.get_json(_BULK_API_URL, params=params)
    raw = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        return []
    return [_parse_api_row(r, scrape_date) for r in raw]


def scrape_bulk_deals_daily(session: NSESession | None = None) -> dict:
    session = session or NSESession()
    scrape_date = today_ist()
    today = date.fromisoformat(scrape_date)

    with RunLogger("bulk_deals", scrape_date) as run:
        records = _fetch_api(session, today, today, scrape_date)
        records = [r for r in records if r["symbol"] and r["client_name"]]
        run.set_fetched(len(records))

        n = bulk_upsert(
            "bulk_deals", records,
            conflict_columns=["deal_date", "symbol", "client_name", "buy_sell", "quantity"],
        )
        run.set_upserted(n)
        logger.info("Bulk deals daily: %d upserted", n)
        return {"fetched": len(records), "upserted": n}


def scrape_bulk_deals_historical(
    session: NSESession | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    session = session or NSESession()
    scrape_date = today_ist()
    start = date.fromisoformat(start_date or BACKFILL_START["bulk_deals"])
    end   = date.fromisoformat(end_date or scrape_date)

    total_fetched = total_upserted = 0

    for d in date_range(start.isoformat(), end.isoformat()):
        if d.weekday() >= 5:          # skip weekends (NSE closed)
            continue

        logger.info("Bulk deals archive: %s", d)
        records = _fetch_archive(session, d, scrape_date)
        if records is None:
            logger.debug("No archive data for %s, trying API", d)
            try:
                records = _fetch_api(session, d, d, scrape_date)
            except Exception as exc:  # noqa: BLE001
                logger.error("Bulk deals API failed for %s: %s", d, exc)
                records = []

        records = [r for r in records if r["symbol"] and r["client_name"]]
        total_fetched += len(records)

        if records:
            n = bulk_upsert(
                "bulk_deals", records,
                conflict_columns=["deal_date", "symbol", "client_name", "buy_sell", "quantity"],
            )
            total_upserted += n

    logger.info("Bulk deals historical done: %d fetched, %d upserted", total_fetched, total_upserted)
    return {"fetched": total_fetched, "upserted": total_upserted}
