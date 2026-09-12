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

## 8. Reports

Two emailed reports read the same fact tables. Both ship a market-cap-focused
HTML body plus a comprehensive PDF and raw CSVs, both render through
`reports/design.py` so the house style is shared rather than copied, and both
claim a `report_log` slot before sending so duplicate cron triggers cannot
produce duplicate mail.

| | Daily | Weekly |
|---|---|---|
| Module | `reports/daily_deals_report.py` | `reports/weekly_deals_report.py` |
| Workflow | `.github/workflows/daily_report.yml` | `.github/workflows/weekly_report.yml` |
| Schedule | Tue–Sat 10:00 IST | Sat 10:00 IST |
| Covers | the previous trading day | the Mon–Fri week just finished |
| `report_log.report_type` | `daily_deals_email` | `weekly_deals_email` |
| Slot key | that trading day | the week's **last** trading day |

The weekly is not five dailies concatenated. Ten of its twelve sections reach
the email body; **XI and XII are inline SVG and therefore PDF-only**, because
Outlook's Word engine renders no SVG at all and would leave a section heading
over blank space. Every chart in the body is built from nested `<td bgcolor>`
cells for the same reason — Word ignores `width` on a div, and Outlook blocks
images until the reader opts in. `_build_html` gates the SVG pair on
`not is_focus`; that gate is a statement about the medium, not a preference.

Five of its sections answer questions a single session cannot:

- **Concentration (II)** — the week's names by rupee value with a running
  share beside each, so a heavy week and a week carrying three heavy prints
  stop looking identical. The max-of-sides rule is applied per feed per
  session, matching `_daily_trend`'s decomposition exactly, so this section
  sums to the same week total the session chart above it prints. Taking the max
  over the whole week instead is the obvious shortcut and is wrong by ~30 cr on
  17–21 Aug 2026 — pinned by a test, because a different total under two
  adjacent sections costs the reader both numbers.
- **Cumulative foreign against domestic (IV)** — the running net per session
  for FII and DII/MF on one shared scale, above the per-session grid. The grid
  says who was on which side each day; it cannot say whether five bars were one
  exit or a build, because nobody sums bars in their head. A class with no deal
  on a session carries its previous total forward rather than resetting: a day
  without a trade is a day the position did not change. Final values reconcile
  exactly against the week-net table beneath — asserted.
- **Persistent flows** — client-symbol pairs traded on more than one session,
  net of what cancelled out. A `conviction` floor (`PERSIST_MIN_CONVICTION`,
  default 0.25) separates a position being built from an HFT round trip, and
  the excluded round-trip count is stated rather than silently dropped.
- **Net flows by client class** — a class × session grid of diverging bars over
  the week-total table. The table alone cannot distinguish one Wednesday block
  from four days of steady selling, and those are different events: on
  17–21 Aug 2026 the FII week-net of −₹5,476 cr was actually a ₹1,482 cr *buy*
  on Tuesday followed by three days of selling, while DII/MF bought every
  session. Bars diverge from a centre rule (left = net sell, right = net buy)
  on one shared scale, so a row reads as a sequence and a column as that
  session's balance of participants. Direction is encoded by both side and
  colour, so it survives greyscale and red-green colour blindness. Row sums
  reconcile exactly against the table beneath — pinned by a test, because a
  chart that disagrees with its own table is worse than no chart.
  Class totals do not sum to zero, because both legs of a deal are reported
  only when each independently crosses the threshold; the table says so.

  This section is the one that depends most on `reports/client_class.py` being
  right, because it sums per-trade inferences into a headline. Reviewing the
  17–21 Aug 2026 edition caught the consequence: the Anglophone corporate-suffix
  list meant `BAYER AG`, `CREDITACCESS INDIA B.V.` and `RESILIENT ASSET
  MANAGEMENT B V` (the renamed Antfin vehicle that held Paytm) all fell through
  to `HNI` — the one class that asserts the holder is a private individual.
  Resilient alone was 101% of the reported HNI net, so the report stated that
  individuals sold ₹5,819 cr when the truth was ₹12.7 cr and one Dutch holding
  company. `_FOREIGN_CORP_FORM` now catches those forms for `CORP`, and the FII
  rule claims genuinely fund-shaped foreign vehicles (Singapore `VCC`, German
  `FONDS`, university endowments). `confidence()` returning `fallback` is the
  query that found this; a suffix match therefore counts as `pattern`, so
  `fallback` keeps meaning "the name carries no signal at all".
- **First appearances** — names and clients absent from the deal feeds for the
  prior `REPORT_WEEKLY_LOOKBACK_WEEKS` (default 4) **and** carrying at least
  `REPORT_WEEKLY_FIRST_MIN_CR` (default ₹100 cr). A failed lookback suppresses
  the section rather than declaring the whole week new. The floor exists because
  roughly half the names in any week are absent from a 4-week window — on
  17–21 Aug 2026 it was 71 of 121 names and 124 of 225 clients, and 108 of those
  clients traded one name on one session. That is the shape of the feed, not
  news. The floor applies to the email body only; the comprehensive PDF lists
  every absent name at any size, and the body states how many it set aside.
- **Session-by-session trend** — every trading day keeps a row even when it
  carried nothing, so a hole in the middle of the week is visible.

`_missing_sessions()` is the weekly-only integrity guard: a trading day with no
bulk **and** no block rows understates every total in the report, and one
missing session is invisible in a daily (that day's email simply said "none")
but obvious across five. It flags the subject line, adds a lead callout, and
exits non-zero so the run goes red — the same trade-off the daily makes, since
a labelled partial read beats no read.

Regenerating and reviewing without sending anything:

```bash
# Previews: PATH.html (email body), PATH_comprehensive.html + .pdf
python -m reports.weekly_deals_report --preview out/week

# Any past week, by any date inside it
python -m reports.weekly_deals_report --week-of 2026-08-19 --preview out/aug17

# The exact bytes that would go over SMTP, as a .eml — body plus the
# comprehensive PDF and CSVs, openable in any mail client. This is the way to
# review a whole edition: the body as the reader sees it and the attachment it
# points at, in one file.
python -m reports.weekly_deals_report --week-of 2026-08-19 --eml out/week.eml

# Pin the weekly aggregations
python -m pytest tests/test_weekly_deals_report.py -q
```
