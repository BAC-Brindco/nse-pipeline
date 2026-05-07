"""
PIT (Prohibition of Insider Trading) Disclosures scraper.

NSE hosts SEBI PIT / SAST (Substantial Acquisition of Shares & Takeovers)
disclosures filed by promoters and insiders.

API (date-range pagination, max ~7-day windows recommended):
  https://www.nseindia.com/api/corporates-pit?index=equities
      &from_date=DD-MM-YYYY&to_date=DD-MM-YYYY

For historical backfill we slide a window from BACKFILL_START["pit"] to today.
"""

import logging
from datetime import date, timedelta

from scrapers.nse_session import NSESession
from database.client import bulk_upsert, RunLogger
from utils.helpers import clean_str, clean_date, clean_numeric, buy_sell_flag, today_ist
from config import BACKFILL_START

logger = logging.getLogger(__name__)

_PIT_URL = "https://www.nseindia.com/api/corporates-pit"
_WINDOW_DAYS = 7


def _api_date(d: date) -> str:
    return d.strftime("%d-%m-%Y")


def _parse_pit_record(row: dict, scrape_date: str) -> dict:
    return {
        "symbol":               clean_str(row.get("symbol") or row.get("Symbol")),
        "company_name":         clean_str(row.get("company") or row.get("companyName") or row.get("issuerName")),
        "isin":                 clean_str(row.get("isin") or row.get("ISIN")),
        "acquirer_name":        clean_str(row.get("acqName") or row.get("acquirerName") or row.get("name")),
        "acquirer_category":    clean_str(row.get("personCategory") or row.get("acqCategory")),
        "regulation":           clean_str(row.get("regulation") or row.get("Regulation")),
        "acq_disp":             buy_sell_flag(row.get("secAcq") or row.get("buyOrSell") or row.get("acquistionDisposal")),
        "before_acq_shares":    None,  # not always present
        "before_acq_pct":       clean_numeric(row.get("befAcqSharesNo") or row.get("beforeAcqSharePercentage")),
        "acq_disp_shares":      None,
        "acq_disp_pct":         clean_numeric(row.get("secAcqDisp") or row.get("acquiredDisposedShares")),
        "after_acq_shares":     None,
        "after_acq_pct":        clean_numeric(row.get("afterAcqSharesNo") or row.get("afterAcqSharePercentage")),
        "transaction_type":     clean_str(row.get("tdpTransactionType") or row.get("modeOfAcq")),
        "date_of_allotment":    clean_date(row.get("date") or row.get("dateOfAllotment") or row.get("acqfromDt")),
        "date_of_intimation":   clean_date(row.get("intimDt") or row.get("dateOfIntimation")),
        "mode_of_acq":          clean_str(row.get("modeOfAcq") or row.get("tdpTransactionType")),
        "exchange":             clean_str(row.get("exchange") or "NSE"),
        "remarks":              clean_str(row.get("remarks") or row.get("Remarks")),
        "scrape_date":          scrape_date,
    }


def _fetch_window(session: NSESession, from_dt: date, to_dt: date) -> list[dict]:
    params = {
        "index": "equities",
        "from_date": _api_date(from_dt),
        "to_date": _api_date(to_dt),
    }
    payload = session.get_json(_PIT_URL, params=params)
    raw = payload.get("data", payload) if isinstance(payload, dict) else payload
    return raw if isinstance(raw, list) else []


def scrape_pit_daily(session: NSESession | None = None) -> dict:
    session = session or NSESession()
    scrape_date = today_ist()
    today = date.fromisoformat(scrape_date)

    with RunLogger("pit", scrape_date) as run:
        raw_rows = _fetch_window(session, today, today)
        records = [_parse_pit_record(r, scrape_date) for r in raw_rows]
        records = [r for r in records if r["symbol"]]
        run.set_fetched(len(records))

        n = bulk_upsert("pit_disclosures", records)
        run.set_upserted(n)
        logger.info("PIT daily: %d records upserted", n)
        return {"fetched": len(records), "upserted": n}


def scrape_pit_historical(
    session: NSESession | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    session = session or NSESession()
    scrape_date = today_ist()
    start = date.fromisoformat(start_date or BACKFILL_START["pit"])
    end = date.fromisoformat(end_date or scrape_date)

    total_fetched = total_upserted = 0
    cursor = start

    while cursor <= end:
        window_end = min(cursor + timedelta(days=_WINDOW_DAYS - 1), end)
        logger.info("PIT window %s → %s", cursor, window_end)

        try:
            raw_rows = _fetch_window(session, cursor, window_end)
            records = [_parse_pit_record(r, scrape_date) for r in raw_rows]
            records = [r for r in records if r["symbol"]]
            total_fetched += len(records)

            if records:
                n = bulk_upsert("pit_disclosures", records)
                total_upserted += n

        except Exception as exc:  # noqa: BLE001
            logger.error("PIT window %s failed: %s", cursor, exc)

        cursor = window_end + timedelta(days=1)

    logger.info("PIT historical: %d fetched, %d upserted", total_fetched, total_upserted)
    return {"fetched": total_fetched, "upserted": total_upserted}
