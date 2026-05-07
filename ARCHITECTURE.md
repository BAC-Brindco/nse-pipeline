# NSE Surveillance Pipeline — Architecture

> Status as of 2026-05-07. Sections marked **[BUILT]** ship in this commit.
> Sections marked **[P0/P1/P2]** are roadmap items prioritized for an
> institutional quantitative desk.

## 1. Why this rewrite

The previous pipeline had three independent bugs that compounded into
total failure: missing Brotli decompression silently corrupted every
JSON response, NSE's 2024-2025 bot-detection upgrade broke the warmup
chain (Python's TLS fingerprint is recognizable), and the bulk/block
backfill walked one calendar day at a time across 25 years against a
removed endpoint. The rebuild solves the failure modes and lays a base
that a quant desk can actually depend on.

## 2. What ships now [BUILT]

### 2.1 Transport layer (`scrapers/nse_session.py`)
- **`curl_cffi` with Chrome 124 TLS impersonation.** Defeats the JA3
  fingerprint check that 403'd vanilla `requests`. Falls back to
  `requests` if `curl_cffi` is unavailable, with a loud warning.
- **Native Brotli + gzip + deflate decompression.**
- **Cookie-validated warmup.** Walks /, /option-chain, /market-data/large-deals
  and asserts at least one of `nsit | nseappid | bm_sv | ak_bmsc` is set
  before returning. Initial connect raises `NSESessionWarmupError` if
  the chain produced no auth cookies — fail loud, not silent.
- **Adaptive re-warm.** Periodic refresh every 50 requests + reactive
  refresh on any 401/403 response.
- **Backwards-compatible API.** All existing scrapers use `.get()` and
  `.get_json()` unchanged.

### 2.2 Backfill engine
- **Range API** (`/api/historical/cm/bulk_deals`,
  `/api/historical/cm/block_deals`) walked in **90-day windows** per
  dataset — quarterly chunks stay inside NSE's fast path and bound
  response payloads. Single-day loops are gone.
- **Multi-source fallback.** Range API → archive CDN CSV per
  trading day. The archive is canonical for older years where the API
  thins out (NSE's public API only goes ~14 years deep).
- **Trading-day calendar** (`nse_trading_holidays` table + helpers
  `is_trading_day`, `iter_trading_days`). Holidays are scraped daily
  from `/api/holiday-master?type=trading`. Backfill skips weekends and
  every published CM-segment holiday — eliminates spurious 404s.
- **Resume from checkpoint** (`backfill_checkpoint` table). Each
  dataset records its last fully-completed window date. Re-running
  resumes from there. Combined with idempotent upserts, a crash mid-run
  is recoverable just by re-invoking the same command.

### 2.3 Audit + provenance
- Every fact table now carries `data_source` (`snapshot` |
  `historical_api` | `archive_csv`) and `source_url`. The desk can
  reconcile divergences when the same trade appears in multiple
  sources with different fields, and can attribute discrepancies to a
  specific upstream payload.
- `scrape_run_log` is unchanged but its semantics are stronger now
  that windows are committed atomically before checkpoint advance.

## 3. Critical institutional gaps [P0]

These break quant strategies silently. They should ship before the
data layer is treated as production.

### 3.1 Equity master with symbol-change history [P0]
**Problem.** Symbols are not stable identifiers in Indian equities.
WIPRO's split-off of WCT, TATA MOTORS' DVR conversion, and routine
name changes all break joins. A bulk deal in `BAJAJ-AUTO` from 2008
points to a different security than today's `BAJAJ-AUTO`.
**Solution.** Table `equity_master(isin, symbol, series, name,
listing_date, delisting_date, ...)` populated from `EQUITY_L.csv` daily
PLUS `symbol_change_log(isin, old_symbol, new_symbol, change_date)`
populated from NSE corporate-actions feed. All fact tables join via
ISIN, not symbol.

### 3.2 Corporate actions [P0]
**Problem.** Splits, bonuses, and consolidations make every quantity
field in `bulk_deals` / `block_deals` time-locally meaningful but
globally nonsense. A 10:1 bonus in 2018 means a 1M-share block deal
from 2017 is really 10M shares in today's units.
**Solution.** Table `corporate_actions(isin, ex_date, action_type,
ratio, ...)` from `/api/corporates-corporateActions`. Materialized
view `bulk_deals_adjusted` exposes split-adjusted quantities and
prices. Quant strategies should query the adjusted view exclusively.

### 3.3 Trading-holidays-aware data freshness SLA [P0]
**Problem.** The desk needs an alert when today's data didn't land
by 17:30 IST on a trading day. The current `scrape_run_log` records
runs but no monitoring consumes it.
**Solution.** A simple cron'd `freshness_check.py` that queries
`max(scrape_date)` per table on every trading day and pages oncall
if stale. Output a single `pipeline_health` view in Supabase that
the desk can dashboard.

## 4. High-value additions [P1]

### 4.1 Point-in-time reconstruction
Quant backtests need to know what the ASM/GSM/T2T list looked like
on any given historical date — not what it looks like today. Solution:
treat ASM/GSM/T2T as slowly-changing dimensions with per-snapshot
tracking. Add `snapshot_id BIGINT` to `asm_list` / `gsm_list` and a
`security_status_history(isin, status_type, status, valid_from,
valid_to)` materialized view. A backtest as-of date `D` queries
`status @ D`, not `status now()`.

### 4.2 F&O security master + open-interest history
For desks running cross-segment strategies (e.g. cash-futures basis
arbitrage), the cash-only schema is half a dataset. Add `fo_master`
(strikes, expiries, lot sizes) and `daily_oi` from
`/api/liveEquity-derivatives` and the F&O bhavcopy.

### 4.3 Sector / industry classification
NSE's sectoral indices give crude classification. NSE also publishes
GICS-aligned classifications via NIFTY 500 constituents. Adding a
`security_classification(isin, sector, industry, market_cap_band)`
table unlocks sector-neutral strategies and crowding analytics.

### 4.4 Bhavcopy ingestion (daily OHLCV + delivery)
The single highest-value dataset NSE publishes is the daily Bhavcopy
(`/content/historical/EQUITIES/{YYYY}/{MMM}/cm{DDMMMYYYY}bhav.csv.zip`)
plus the security-deliverable file (`MTO_{DDMMYYYY}.DAT`). Together
they give clean OHLCV + delivery percentage — the primary inputs for
most equity strategies. The current pipeline doesn't capture this.

## 5. Operational hardening [P2]

### 5.1 Secondary source strategy
Single-source dependency on NSE is fragile. The architecture supports
adding parallel scrapers (BSE, Moneycontrol, NSEpython library) with
divergence detection on `data_source` column. Recommended for
mission-critical fields (deal_date, quantity, price) where a desk
trade decision depends on the value.

### 5.2 Concurrency
Backfill is currently serial. With cookie warmup amortized across a
session, 4-way concurrent windows would cut backfill time by ~3.5x.
Use `asyncio` + `curl_cffi.requests.AsyncSession`. Cap at 4 parallel
to stay under NSE's per-IP rate limit.

### 5.3 Proxy rotation
For production-grade resilience under sustained scraping, route
through a residential proxy pool (BrightData, Oxylabs). Already
plumbed via `NSE_PROXY_URL`; needs a rotator wrapper.

### 5.4 Anomaly detection on row counts
After every successful daily run, write `(dataset, scrape_date,
row_count)` to a metrics table. Z-score against the trailing 30-day
distribution; alert on z > 3 (suggests an upstream schema change or
a partial scrape that passed status=success).

### 5.5 Schema evolution policy
Every fact table should grow only via `ALTER TABLE ADD COLUMN ... NULL`
to preserve append-only consumer compatibility. Backfilling new
columns from existing rows requires a separate migration job.

## 6. Known limitations of NSE as a source

- **Bulk-deal data starts ~Sept 2009.** Pre-SEBI mandate; nothing
  exists upstream.
- **Block-deal data starts ~Jan 2010.**
- **PIT disclosures start 2015-05-15** (SEBI PIT Regulations effective
  date).
- **T2T has no historical archive.** `EQUITY_L.csv` is current state
  only. Reconstructing T2T-status-as-of-historical-date requires
  daily snapshots compounded over time. This pipeline starts that
  process; a true historical T2T view will only become available
  ~12-18 months after first daily snapshot lands.
- **NSE archive CDN naming is inconsistent across years.** The
  `archive_urls()` helpers try multiple naming conventions; a few
  pre-2010 dates may need manual scraping with a fourth pattern not
  yet seen in the wild.

## 7. Quick reference — running the pipeline

```bash
# One-time: apply schema (creates new tables: holidays, checkpoint, etc.)
python database/apply_schema.py

# First-time historical seed (resumable; safe to re-run)
python historical_backfill.py

# Subset / range
python historical_backfill.py --datasets bulk_deals pit
python historical_backfill.py --datasets bulk_deals --start 2020-01-01 --end 2024-12-31

# Force re-fetch (ignore checkpoint)
python historical_backfill.py --no-resume

# Daily run (also wired to GitHub Actions cron)
python main.py
python main.py --datasets holidays asm gsm pit
```
