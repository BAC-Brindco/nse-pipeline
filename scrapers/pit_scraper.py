"""
PIT (Prohibition of Insider Trading) Disclosures scraper.

NSE exposes TWO related PIT endpoints we union over:
  /api/corporates-pit       → primary disclosures (Reg 7(2) etc.)
  /api/corporates-pit-gg    → "GG" subset; some installations expose
                              a different slice — including both gives
                              us the maximum coverage.

Each endpoint returns JSON keyed by `data: [...]` with field names that
have changed across NSE's site rewrites. We use a wide candidate-list
parser AND store the entire raw record in `raw_payload` (JSONB) so that
even if our parser misses fields, no upstream data is lost — we can
re-derive parsed columns later from raw_payload alone.

Segments per endpoint:
  equities    — main NSE board (bulk of disclosures)
  sme         — NSE Emerge platform
  invitsreits — InvITs and REITs
"""

import logging
from datetime import date, timedelta
from typing import Any

from scrapers.nse_session import NSESession
from database.client import bulk_upsert, RunLogger, get_checkpoint, set_checkpoint
from utils.helpers import clean_str, clean_date, clean_numeric, buy_sell_flag, today_ist
from config import BACKFILL_START

logger = logging.getLogger(__name__)

_PIT_URLS = (
    "https://www.nseindia.com/api/corporates-pit",
    "https://www.nseindia.com/api/corporates-pit-gg",
)
_SEGMENTS = ("equities", "sme", "invitsreits")
_WINDOW_DAYS = 90


def _api_date(d: date) -> str:
    return d.strftime("%d-%m-%Y")


def _first(row: dict, *keys: str) -> Any:
    """Return the first non-None value in `row` for any of the given keys."""
    for k in keys:
        v = row.get(k)
        if v not in (None, "", "-"):
            return v
    return None


def _parse_pit_record(row: dict, segment: str, scrape_date: str, source_url: str) -> dict:
    # Wide candidate list — NSE has shipped at least 4 different field-name
    # conventions for these endpoints over the years. We try every known
    # variant and fall back to raw_payload for forensic recovery.
    return {
        "symbol":               clean_str(_first(row, "symbol", "Symbol", "SYMBOL", "scripCode")),
        "company_name":         clean_str(_first(row, "company", "companyName", "issuerName",
                                                 "nameOfCompany", "anyOtherCompany")),
        "isin":                 clean_str(_first(row, "isin", "ISIN", "scripIsinNo")),
        "acquirer_name":        clean_str(_first(row, "acqName", "acquirerName", "name",
                                                 "nameOfThePerson", "namePerson",
                                                 "personName", "anyPerson",
                                                 "personDP")),
        "acquirer_category":    clean_str(_first(row, "personCategory", "acqCategory",
                                                 "categoryOfPerson", "categoryPerson",
                                                 "categoryName")),
        "regulation":           clean_str(_first(row, "regulation", "Regulation",
                                                 "regulationName")),
        "acq_disp":             buy_sell_flag(_first(row, "secAcq", "buyOrSell",
                                                     "acquistionDisposal",
                                                     "acquisitionDisposal",
                                                     "modeOfAcq", "modeOfAcquisition",
                                                     "buySellTradeType", "tdpTransactionType")),
        "before_acq_shares":    None,  # parsed from before_acq fields below if numeric
        "before_acq_pct":       clean_numeric(_first(row, "befAcqSharesNo",
                                                     "beforeAcqSharePercentage",
                                                     "befAcqShareholdingPercentage",
                                                     "shareholdingPriorPct",
                                                     "preTransactionPct")),
        "acq_disp_shares":      clean_numeric(_first(row, "secAcqDisp",
                                                     "acquiredDisposedSharesShares",
                                                     "noOfShares",
                                                     "noOfSharesAcquiredOrDisposed",
                                                     "shareCount", "qty")),
        "acq_disp_pct":         clean_numeric(_first(row, "secAcqDispPct",
                                                     "acquiredDisposedSharesPercentage",
                                                     "shareholdingPercentage",
                                                     "noOfSharesPercentage",
                                                     "transactionPct")),
        "after_acq_shares":     None,
        "after_acq_pct":        clean_numeric(_first(row, "afterAcqSharesNo",
                                                     "afterAcqSharePercentage",
                                                     "afterAcqShareholdingPercentage",
                                                     "shareholdingAfterPct",
                                                     "postTransactionPct")),
        "transaction_type":     clean_str(_first(row, "tdpTransactionType",
                                                 "modeOfAcq", "modeOfAcquisition",
                                                 "transactionType")),
        "date_of_allotment":    clean_date(_first(row, "date", "dateOfAllotment",
                                                  "acqfromDt", "fromDate",
                                                  "transactionDate",
                                                  "dateOfAcquisition",
                                                  "dateOfAllotmentAdvice")),
        "date_of_intimation":   clean_date(_first(row, "intimDt", "dateOfIntimation",
                                                  "intimationDate",
                                                  "dateOfIntimationToCompany",
                                                  "intimationDateToCompany",
                                                  "dt")),
        "mode_of_acq":          clean_str(_first(row, "modeOfAcq", "modeOfAcquisition",
                                                 "tdpTransactionType",
                                                 "modeOfAcquisitionDisposal")),
        "exchange":             clean_str(_first(row, "exchange") or "NSE"),
        "segment":              segment,
        "remarks":              clean_str(_first(row, "remarks", "Remarks", "note")),
        "data_source":          "historical_api",
        "source_url":           source_url,
        "raw_payload":          row,  # JSONB — full upstream record for forensic recovery
        "scrape_date":          scrape_date,
    }


_PIT_REFERER = "https://www.nseindia.com/companies-listing/corporate-filings-insider-trading"


def _fetch_window(session: NSESession, base_url: str, segment: str,
                  from_dt: date, to_dt: date) -> tuple[list[dict], str]:
    params = {
        "index":     segment,
        "from_date": _api_date(from_dt),
        "to_date":   _api_date(to_dt),
    }
    url_with_params = (
        f"{base_url}?index={segment}&from_date={_api_date(from_dt)}"
        f"&to_date={_api_date(to_dt)}"
    )
    payload = session.get_json(base_url, params=params, headers={"Referer": _PIT_REFERER})
    raw = payload.get("data", payload) if isinstance(payload, dict) else payload
    rows = raw if isinstance(raw, list) else []
    if rows:
        logger.info("PIT [%s] %s → %s: %d rows from %s",
                    segment, from_dt, to_dt, len(rows),
                    base_url.rsplit("/", 1)[-1])
    return rows, url_with_params


def _scrape_segment_window(session: NSESession, segment: str, from_dt: date, to_dt: date,
                           scrape_date: str) -> list[dict]:
    """Union over both endpoints for a (segment, window). Dedup by raw payload."""
    seen: set[str] = set()
    out: list[dict] = []
    for base_url in _PIT_URLS:
        try:
            raw_rows, src = _fetch_window(session, base_url, segment, from_dt, to_dt)
        except Exception as exc:  # noqa: BLE001
            logger.warning("PIT %s @ %s [%s → %s] failed: %s",
                           segment, base_url.rsplit("/", 1)[-1], from_dt, to_dt, exc)
            continue
        for r in raw_rows:
            key = repr(sorted(r.items())) if isinstance(r, dict) else repr(r)
            if key in seen:
                continue
            seen.add(key)
            rec = _parse_pit_record(r, segment, scrape_date, src)
            if rec.get("symbol"):
                out.append(rec)
    return out


def scrape_pit_daily(session: NSESession | None = None) -> dict:
    session = session or NSESession()
    scrape_date = today_ist()
    today = date.fromisoformat(scrape_date)
    yesterday = today - timedelta(days=1)

    total_fetched = total_upserted = 0

    with RunLogger("pit", scrape_date) as run:
        for segment in _SEGMENTS:
            try:
                # Use a 2-day window to guard against late filings dated yesterday
                records = _scrape_segment_window(session, segment, yesterday, today, scrape_date)
                total_fetched += len(records)
                if records:
                    n = bulk_upsert("pit_disclosures", records)
                    total_upserted += n
            except Exception as exc:  # noqa: BLE001
                logger.error("PIT daily %s failed: %s", segment, exc)

        run.set_fetched(total_fetched)
        run.set_upserted(total_upserted)

    logger.info("PIT daily: %d fetched, %d upserted", total_fetched, total_upserted)
    return {"fetched": total_fetched, "upserted": total_upserted}


def scrape_pit_historical(
    session: NSESession | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    resume: bool = True,
) -> dict:
    session = session or NSESession()
    scrape_date = today_ist()

    if resume and start_date is None:
        ck = get_checkpoint("pit")
        if ck:
            start = ck + timedelta(days=1)
            logger.info("Resuming pit backfill from checkpoint %s", start)
        else:
            start = date.fromisoformat(BACKFILL_START["pit"])
    else:
        start = date.fromisoformat(start_date or BACKFILL_START["pit"])

    end = date.fromisoformat(end_date or scrape_date)
    if start > end:
        logger.info("pit: nothing to backfill (start %s > end %s)", start, end)
        return {"fetched": 0, "upserted": 0}

    total_fetched = total_upserted = 0

    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=_WINDOW_DAYS - 1), end)
        window_records: list[dict] = []

        for segment in _SEGMENTS:
            logger.info("PIT [%s] window %s → %s", segment, cursor, window_end)
            try:
                records = _scrape_segment_window(session, segment, cursor, window_end, scrape_date)
                window_records.extend(records)
            except Exception as exc:  # noqa: BLE001
                logger.error("PIT [%s] window %s failed: %s", segment, cursor, exc)

        total_fetched += len(window_records)
        if window_records:
            n = bulk_upsert("pit_disclosures", window_records)
            total_upserted += n
            logger.info("PIT window %s → %s: %d rows", cursor, window_end, n)

        set_checkpoint("pit", window_end, rows_added=total_upserted)
        cursor = window_end + timedelta(days=1)

    logger.info("PIT historical done: %d fetched, %d upserted", total_fetched, total_upserted)
    return {"fetched": total_fetched, "upserted": total_upserted}
