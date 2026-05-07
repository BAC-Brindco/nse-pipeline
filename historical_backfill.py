"""
Historical backfill runner.

Pulls maximum available history for each dataset.
Designed for repeated invocation — every run resumes from the
last persisted backfill_checkpoint per dataset (idempotent thanks to
upserts), so a partial run + re-run = full history with no gaps.

Usage:
    python historical_backfill.py                                    # all datasets, resume
    python historical_backfill.py --datasets bulk_deals pit
    python historical_backfill.py --datasets bulk_deals --start 2010-01-01 --end 2015-12-31
    python historical_backfill.py --no-resume                        # ignore checkpoint
"""

import argparse
import logging
import sys
from datetime import datetime

import pytz

from scrapers.nse_session import NSESession
from scrapers.holidays_scraper import scrape_holidays
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

_ALL_DATASETS = ["holidays", "asm", "gsm", "t2t", "pit", "bulk_deals", "block_deals"]


def run(
    datasets: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    resume: bool = True,
):
    ist = pytz.timezone("Asia/Kolkata")
    logger.info("NSE historical backfill starting at %s IST", datetime.now(ist).isoformat())

    session = NSESession()
    targets = datasets or _ALL_DATASETS

    # Always refresh the holiday calendar before any range-based scraping
    # so iter_trading_days() has the latest list. Cheap (~1 request).
    if "holidays" in targets:
        try:
            logger.info("══ Refreshing trading holiday calendar ══")
            scrape_holidays(session)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Holiday refresh failed (will fall back to weekend-only): %s", exc)

    results = {}

    for name in targets:
        if name == "holidays":
            continue   # handled above
        logger.info("══ Backfilling: %s (from %s) ══", name, start or BACKFILL_START.get(name, "?"))

        try:
            if name == "asm":
                results[name] = scrape_asm(session)

            elif name == "gsm":
                results[name] = scrape_gsm(session)

            elif name == "t2t":
                results[name] = scrape_t2t(session)

            elif name == "pit":
                results[name] = scrape_pit_historical(
                    session,
                    start_date=start,
                    end_date=end,
                    resume=resume,
                )

            elif name == "bulk_deals":
                results[name] = scrape_bulk_deals_historical(
                    session,
                    start_date=start,
                    end_date=end,
                    resume=resume,
                )

            elif name == "block_deals":
                results[name] = scrape_block_deals_historical(
                    session,
                    start_date=start,
                    end_date=end,
                    resume=resume,
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
        choices=_ALL_DATASETS,
        help="Datasets to backfill (default: all)")
    parser.add_argument("--start", help="Override start date YYYY-MM-DD (disables resume)")
    parser.add_argument("--end",   help="Override end date YYYY-MM-DD")
    parser.add_argument("--no-resume", action="store_true",
        help="Ignore backfill_checkpoint and start from BACKFILL_START")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    # Explicit --start always overrides resume (the user wants that range).
    resume = not args.no_resume and args.start is None
    run(args.datasets, args.start, args.end, resume=resume)
