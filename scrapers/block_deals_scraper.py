"""
Block Deals scraper.

A block deal is a single transaction of a minimum quantity of 5 lakh shares
or a minimum value of INR 5 crore, executed in the opening block window
(08:45–09:00 AM IST) on the exchange.

Sources:
  Daily API:  https://www.nseindia.com/api/block-deal
              (params: from_date, to_date in DD-MM-YYYY format)

  Historical archives:
      https://archives.nseindia.com/content/equities/block_deals_{DDMMYYYY}.csv
      https://archives.nseindia.com/content/equities/BLOCK_DEALS_{DD-Mon-YYYY}.csv
"""

import io
import logging
from datetime import date, timedelta

import pandas as pd

from scrapers.nse_session import NSESession
from database.client import bulk_upsert, RunLogger
from utils.helpers import (
    clean_str, clean_date, clean_numeric, clean_int,
    buy_sell_flag, today_ist, date_range,
)
from config import BACKFILL_START, NSE_ARCHIVE_URL

logger = logging.getLogger(__name__)

_BLOCK_API_URL = "https://www.nseindia.com/api/block-deal"


def _archive_urls(d: date) -> list[str]:
    mon3 = d.strftime("%b").capitalize()
    ddmmyyyy = d.strftime("%d%m%Y")
    ddmonyyyy = d.strftime("%d-") + mon3 + d.strftime("-%Y")
    return [
        f"{NSE_ARCHIVE_URL}/content/equities/block_deals_{ddmmyyyy}.csv",
        f"{NSE_ARCHIVE_URL}/content/equities/BLOCK_DEALS_{ddmonyyyy}.csv",
        f"{NSE_ARCHIVE_URL}/content/equities/block_deals_{ddmonyyyy}.csv",
    ]


def _parse_api_row(row: dict, scrape_date: str) -> dict:
    return {
        "deal_date":     clean_date(row.get("date") or row.get("dealDate")),
        "symbol":        clean_str(row.get("symbol") or row.get("Symbol")),
        "security_name": clean_str(row.get("secDesc") or row.get("securityName")),
        "client_name":   clean_str(row.get("clientName") or row.get("clientname")),
        "buy_sell":      buy_sell_flag(row.get("buySell") or row.get("transactionType")),
        "quantity":      clean_int(row.get("qty") or row.get("quantity")),
        "trade_price":   clean_numeric(row.get("price") or row.get("tradePrice")),
        "exchange":      "NSE",
        "scrape_date":   scrape_date,
    }


def _parse_csv_df(df: pd.DataFrame, d: date, scrape_date: str) -> list[dict]:
    df.columns = [c.strip().upper() for c in df.columns]
    col_map = {
        "SYMBOL": "symbol",
        "SECURITY NAME": "security_name",
        "CLIENT NAME": "client_name",
        "BUY/SELL": "buy_sell",
        "QUANTITY TRADED": "quantity",
        "TRADE PRICE": "trade_price",
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
            "trade_price":   clean_numeric(str(row.get("trade_price", ""))),
            "exchange":      "NSE",
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
            logger.debug("Block archive %s failed: %s", url, exc)
    return None


def _fetch_api(session: NSESession, from_dt: date, to_dt: date, scrape_date: str) -> list[dict]:
    params = {
        "from_date": from_dt.strftime("%d-%m-%Y"),
        "to_date":   to_dt.strftime("%d-%m-%Y"),
    }
    payload = session.get_json(_BLOCK_API_URL, params=params)
    raw = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        return []
    return [_parse_api_row(r, scrape_date) for r in raw]


def scrape_block_deals_daily(session: NSESession | None = None) -> dict:
    session = session or NSESession()
    scrape_date = today_ist()
    today = date.fromisoformat(scrape_date)

    with RunLogger("block_deals", scrape_date) as run:
        records = _fetch_api(session, today, today, scrape_date)
        records = [r for r in records if r["symbol"] and r["client_name"]]
        run.set_fetched(len(records))

        n = bulk_upsert(
            "block_deals", records,
            conflict_columns=["deal_date", "symbol", "client_name", "buy_sell", "quantity"],
        )
        run.set_upserted(n)
        logger.info("Block deals daily: %d upserted", n)
        return {"fetched": len(records), "upserted": n}


def scrape_block_deals_historical(
    session: NSESession | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    session = session or NSESession()
    scrape_date = today_ist()
    start = date.fromisoformat(start_date or BACKFILL_START["block_deals"])
    end   = date.fromisoformat(end_date or scrape_date)

    total_fetched = total_upserted = 0

    for d in date_range(start.isoformat(), end.isoformat()):
        if d.weekday() >= 5:
            continue

        logger.info("Block deals archive: %s", d)
        records = _fetch_archive(session, d, scrape_date)
        if records is None:
            try:
                records = _fetch_api(session, d, d, scrape_date)
            except Exception as exc:  # noqa: BLE001
                logger.error("Block deals API failed for %s: %s", d, exc)
                records = []

        records = [r for r in records if r["symbol"] and r["client_name"]]
        total_fetched += len(records)

        if records:
            n = bulk_upsert(
                "block_deals", records,
                conflict_columns=["deal_date", "symbol", "client_name", "buy_sell", "quantity"],
            )
            total_upserted += n

    logger.info("Block deals historical done: %d fetched, %d upserted", total_fetched, total_upserted)
    return {"fetched": total_fetched, "upserted": total_upserted}
