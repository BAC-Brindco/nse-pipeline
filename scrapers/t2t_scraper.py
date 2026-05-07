"""
T2T (Trade-to-Trade / BE series) scraper.

Securities in the T2T segment must be compulsorily settled on a
gross basis (delivery only) — intraday squaring-off is not permitted.

Source:
  https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv
  Full equity listing file; T2T stocks have SERIES = 'BE'.
  Static CDN file — no session or cookies required.

Note: DATE OF LISTING is used as date_of_addition (best stable proxy
available; the actual T2T re-classification date is not in this file).
"""

import io
import logging

import pandas as pd

from scrapers.nse_session import NSESession
from database.client import bulk_upsert, RunLogger
from utils.helpers import clean_str, clean_date, today_ist

logger = logging.getLogger(__name__)

_EQUITY_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"


def _parse_csv(df: pd.DataFrame, scrape_date: str) -> list[dict]:
    df.columns = [c.strip() for c in df.columns]

    be_df = df[df["SERIES"].str.strip() == "BE"].copy()

    records = []
    for _, row in be_df.iterrows():
        records.append({
            "symbol":           clean_str(str(row.get("SYMBOL", ""))),
            "series":           "BE",
            "company_name":     clean_str(str(row.get("NAME OF COMPANY", ""))),
            "isin":             clean_str(str(row.get("ISIN NUMBER", ""))),
            "date_of_addition": clean_date(str(row.get("DATE OF LISTING", ""))),
            "date_of_removal":  None,
            "remarks":          None,
            "scrape_date":      scrape_date,
        })
    return records


def scrape_t2t(session: NSESession | None = None) -> dict:
    session = session or NSESession()
    scrape_date = today_ist()

    with RunLogger("t2t", scrape_date) as run:
        try:
            resp = session.get(_EQUITY_LIST_URL)
            df = pd.read_csv(io.StringIO(resp.text))
        except Exception as exc:
            logger.error("T2T: failed to fetch equity list CSV: %s", exc)
            run.fail(str(exc))
            return {"fetched": 0, "upserted": 0}

        records = _parse_csv(df, scrape_date)
        records = [r for r in records if r["symbol"]]
        run.set_fetched(len(records))

        n = bulk_upsert(
            "t2t_list",
            records,
            conflict_columns=["symbol", "series", "date_of_addition"],
        )
        run.set_upserted(n)
        logger.info("T2T: %d BE-series records upserted", n)
        return {"fetched": len(records), "upserted": n}
