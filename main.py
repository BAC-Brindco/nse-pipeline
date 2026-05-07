"""
Daily NSE data pull — runs after market close (16:00 IST / 10:30 UTC).

Pulls current-day data for all 6 datasets in a single shared session.
"""

import argparse
import logging
import sys
from datetime import datetime

import pytz

from scrapers.nse_session import NSESession
from scrapers.asm_scraper import scrape_asm
from scrapers.gsm_scraper import scrape_gsm
from scrapers.t2t_scraper import scrape_t2t
from scrapers.pit_scraper import scrape_pit_daily
from scrapers.bulk_deals_scraper import scrape_bulk_deals_daily
from scrapers.block_deals_scraper import scrape_block_deals_daily

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("nse.daily")

_DATASETS = {
    "asm":         scrape_asm,
    "gsm":         scrape_gsm,
    "t2t":         scrape_t2t,
    "pit":         scrape_pit_daily,
    "bulk_deals":  scrape_bulk_deals_daily,
    "block_deals": scrape_block_deals_daily,
}


def run(datasets: list[str] | None = None):
    ist = pytz.timezone("Asia/Kolkata")
    logger.info("NSE daily scrape starting at %s IST", datetime.now(ist).isoformat())

    session = NSESession()
    targets = datasets or list(_DATASETS.keys())
    results = {}

    for name in targets:
        if name not in _DATASETS:
            logger.warning("Unknown dataset '%s' — skipping", name)
            continue
        logger.info("── Scraping: %s", name)
        try:
            results[name] = _DATASETS[name](session)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Dataset '%s' failed: %s", name, exc)
            results[name] = {"error": str(exc)}

    logger.info("── Summary ──────────────────────────────")
    for name, res in results.items():
        logger.info("  %-15s %s", name, res)
    logger.info("── Done ──────────────────────────────────")
    return results


def _parse_args():
    parser = argparse.ArgumentParser(description="NSE daily data scraper")
    parser.add_argument(
        "--datasets", nargs="*",
        choices=list(_DATASETS.keys()),
        help="Specific datasets to scrape (default: all)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run(args.datasets)
