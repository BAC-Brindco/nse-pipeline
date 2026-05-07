"""
Historical backfill runner.

Pulls maximum available history for each dataset.
This is designed to be run ONCE (or on demand) — daily scrapes
in main.py handle forward incremental updates.

Usage:
    python historical_backfill.py                           # all datasets
    python historical_backfill.py --datasets bulk_deals pit
    python historical_backfill.py --datasets bulk_deals --start 2010-01-01 --end 2015-12-31
"""

import argparse
import logging
import sys
from datetime import datetime

import pytz

from scrapers.nse_session import NSESession
from scrapers.asm_scraper import scrape_asm           # ASM API only has current list; daily snapshots accumulate
from scrapers.gsm_scraper import scrape_gsm
from scrapers.t2t_scraper import scrape_t2t
from scrapers.pit_scraper import scrape_pit_historical
from scrapers.bulk_deals_scraper import scrape_bulk_deals_historical
from scrapers.block_deals_scraper import scrape_block_deals_historical
from config import BACKFILL_START

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("nse.backfill")


def run(datasets: list[str] | None = None, start: str | None = None, end: str | None = None):
    ist = pytz.timezone("Asia/Kolkata")
    logger.info("NSE historical backfill starting at %s IST", datetime.now(ist).isoformat())

    session = NSESession()
    all_datasets = ["asm", "gsm", "t2t", "pit", "bulk_deals", "block_deals"]
    targets = datasets or all_datasets

    results = {}

    for name in targets:
        logger.info("══ Backfilling: %s (from %s) ══", name, start or BACKFILL_START.get(name, "?"))

        try:
            if name == "asm":
                # ASM API only exposes current list; pull it now to seed the table.
                results[name] = scrape_asm(session)

            elif name == "gsm":
                results[name] = scrape_gsm(session)

            elif name == "t2t":
                results[name] = scrape_t2t(session)

            elif name == "pit":
                results[name] = scrape_pit_historical(
                    session,
                    start_date=start or BACKFILL_START["pit"],
                    end_date=end,
                )

            elif name == "bulk_deals":
                results[name] = scrape_bulk_deals_historical(
                    session,
                    start_date=start or BACKFILL_START["bulk_deals"],
                    end_date=end,
                )

            elif name == "block_deals":
                results[name] = scrape_block_deals_historical(
                    session,
                    start_date=start or BACKFILL_START["block_deals"],
                    end_date=end,
                )

        except Exception as exc:  # noqa: BLE001
            logger.exception("Backfill '%s' failed: %s", name, exc)
            results[name] = {"error": str(exc)}

    logger.info("── Backfill Summary ──────────────────────")
    for name, res in results.items():
        logger.info("  %-15s %s", name, res)
    logger.info("── Done ──────────────────────────────────")
    return results


def _parse_args():
    parser = argparse.ArgumentParser(description="NSE historical backfill")
    parser.add_argument("--datasets", nargs="*",
        choices=["asm", "gsm", "t2t", "pit", "bulk_deals", "block_deals"],
        help="Datasets to backfill (default: all)")
    parser.add_argument("--start", help="Override start date YYYY-MM-DD")
    parser.add_argument("--end",   help="Override end date YYYY-MM-DD")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run(args.datasets, args.start, args.end)
