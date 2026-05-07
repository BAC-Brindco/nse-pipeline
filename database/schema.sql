-- ============================================================
-- NSE Surveillance & Market Activity Data - Supabase Schema
-- ============================================================

-- ─────────────────────────────────────────────
-- ASM (Additional Surveillance Measure)
-- Short-term & Long-term lists maintained by NSE
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS asm_list (
    id                  BIGSERIAL PRIMARY KEY,
    symbol              VARCHAR(50)  NOT NULL,
    series              VARCHAR(10)  NOT NULL DEFAULT 'EQ',
    company_name        TEXT,
    isin                VARCHAR(20),
    asm_type            VARCHAR(20)  NOT NULL,   -- 'short_term' | 'long_term'
    stage               VARCHAR(20),             -- stage within ASM (I, II, etc.)
    date_of_addition    DATE,
    date_of_removal     DATE,
    reason              TEXT,
    remarks             TEXT,
    scrape_date         DATE         NOT NULL DEFAULT CURRENT_DATE,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT asm_unique UNIQUE (symbol, series, asm_type, date_of_addition)
);

CREATE INDEX IF NOT EXISTS idx_asm_symbol     ON asm_list (symbol);
CREATE INDEX IF NOT EXISTS idx_asm_scrape_dt  ON asm_list (scrape_date);
CREATE INDEX IF NOT EXISTS idx_asm_type       ON asm_list (asm_type);

-- ─────────────────────────────────────────────
-- GSM (Graded Surveillance Measure)
-- Stages I through VI based on price/volume anomalies
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gsm_list (
    id                  BIGSERIAL PRIMARY KEY,
    symbol              VARCHAR(50)  NOT NULL,
    series              VARCHAR(10)  NOT NULL DEFAULT 'EQ',
    company_name        TEXT,
    isin                VARCHAR(20),
    stage               INTEGER      NOT NULL,   -- 1-6 standard; higher values for IBC/special codes
    date_of_addition    DATE,
    date_of_removal     DATE,
    remarks             TEXT,
    scrape_date         DATE         NOT NULL DEFAULT CURRENT_DATE,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT gsm_unique UNIQUE (symbol, series, stage, date_of_addition)
);

CREATE INDEX IF NOT EXISTS idx_gsm_symbol     ON gsm_list (symbol);
CREATE INDEX IF NOT EXISTS idx_gsm_stage      ON gsm_list (stage);
CREATE INDEX IF NOT EXISTS idx_gsm_scrape_dt  ON gsm_list (scrape_date);

-- ─────────────────────────────────────────────
-- T2T (Trade-to-Trade Settlement)
-- Mandatory delivery; no intraday netting allowed
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS t2t_list (
    id                  BIGSERIAL PRIMARY KEY,
    symbol              VARCHAR(50)  NOT NULL,
    series              VARCHAR(10)  NOT NULL DEFAULT 'BE',
    company_name        TEXT,
    isin                VARCHAR(20),
    date_of_addition    DATE,
    date_of_removal     DATE,
    remarks             TEXT,
    scrape_date         DATE         NOT NULL DEFAULT CURRENT_DATE,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT t2t_unique UNIQUE (symbol, series, date_of_addition)
);

CREATE INDEX IF NOT EXISTS idx_t2t_symbol     ON t2t_list (symbol);
CREATE INDEX IF NOT EXISTS idx_t2t_scrape_dt  ON t2t_list (scrape_date);

-- ─────────────────────────────────────────────
-- PIT Disclosures (Prohibition of Insider Trading)
-- SEBI Regulation disclosures filed on NSE
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pit_disclosures (
    id                          BIGSERIAL PRIMARY KEY,
    symbol                      VARCHAR(50),
    company_name                TEXT,
    isin                        VARCHAR(20),
    acquirer_name               TEXT,
    acquirer_category           VARCHAR(100),   -- promoter, institutional, etc.
    regulation                  VARCHAR(50),    -- SAST Reg 29(1), 29(2), etc.
    acq_disp                    VARCHAR(20),    -- acquisition or disposal
    before_acq_shares           BIGINT,
    before_acq_pct              NUMERIC(10, 6),
    acq_disp_shares             BIGINT,
    acq_disp_pct                NUMERIC(10, 6),
    after_acq_shares            BIGINT,
    after_acq_pct               NUMERIC(10, 6),
    transaction_type            VARCHAR(50),    -- market purchase, off-market, etc.
    date_of_allotment           DATE,
    date_of_intimation          DATE,
    mode_of_acq                 VARCHAR(100),
    exchange                    VARCHAR(20),
    segment                     VARCHAR(20) DEFAULT 'equities',  -- equities | sme | invitsreits
    remarks                     TEXT,
    scrape_date                 DATE        NOT NULL DEFAULT CURRENT_DATE,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pit_symbol      ON pit_disclosures (symbol);
CREATE INDEX IF NOT EXISTS idx_pit_acq_name    ON pit_disclosures (acquirer_name);
CREATE INDEX IF NOT EXISTS idx_pit_date_allot  ON pit_disclosures (date_of_allotment);
CREATE INDEX IF NOT EXISTS idx_pit_scrape_dt   ON pit_disclosures (scrape_date);
CREATE INDEX IF NOT EXISTS idx_pit_segment     ON pit_disclosures (segment);

-- ─────────────────────────────────────────────
-- Bulk Deals
-- Single-client trades >= 0.5% of listed shares
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bulk_deals (
    id                  BIGSERIAL PRIMARY KEY,
    deal_date           DATE         NOT NULL,
    symbol              VARCHAR(50)  NOT NULL,
    security_name       TEXT,
    client_name         TEXT         NOT NULL,
    buy_sell            CHAR(1)      NOT NULL CHECK (buy_sell IN ('B','S')),
    quantity            BIGINT       NOT NULL,
    avg_price           NUMERIC(15, 4),
    exchange            VARCHAR(10)  NOT NULL DEFAULT 'NSE',
    remarks             TEXT,
    scrape_date         DATE         NOT NULL DEFAULT CURRENT_DATE,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT bulk_unique UNIQUE (deal_date, symbol, client_name, buy_sell, quantity)
);

CREATE INDEX IF NOT EXISTS idx_bulk_date      ON bulk_deals (deal_date);
CREATE INDEX IF NOT EXISTS idx_bulk_symbol    ON bulk_deals (symbol);
CREATE INDEX IF NOT EXISTS idx_bulk_client    ON bulk_deals (client_name);
CREATE INDEX IF NOT EXISTS idx_bulk_scrape_dt ON bulk_deals (scrape_date);

-- ─────────────────────────────────────────────
-- Block Deals
-- Negotiated trades >= 5 lakh shares or >= INR 5 Cr
-- executed in opening block window (8:45–9:00 AM)
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS block_deals (
    id                  BIGSERIAL PRIMARY KEY,
    deal_date           DATE         NOT NULL,
    symbol              VARCHAR(50)  NOT NULL,
    security_name       TEXT,
    client_name         TEXT         NOT NULL,
    buy_sell            CHAR(1)      NOT NULL CHECK (buy_sell IN ('B','S')),
    quantity            BIGINT       NOT NULL,
    trade_price         NUMERIC(15, 4),
    exchange            VARCHAR(10)  NOT NULL DEFAULT 'NSE',
    scrape_date         DATE         NOT NULL DEFAULT CURRENT_DATE,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT block_unique UNIQUE (deal_date, symbol, client_name, buy_sell, quantity)
);

CREATE INDEX IF NOT EXISTS idx_block_date      ON block_deals (deal_date);
CREATE INDEX IF NOT EXISTS idx_block_symbol    ON block_deals (symbol);
CREATE INDEX IF NOT EXISTS idx_block_client    ON block_deals (client_name);
CREATE INDEX IF NOT EXISTS idx_block_scrape_dt ON block_deals (scrape_date);

-- ─────────────────────────────────────────────
-- Short Selling Reports
-- Daily short positions reported by members
-- (SHORT_DEALS_DATA from snapshot-capital-market-largedeal)
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS short_deals (
    id                  BIGSERIAL PRIMARY KEY,
    deal_date           DATE         NOT NULL,
    symbol              VARCHAR(50)  NOT NULL,
    security_name       TEXT,
    quantity            BIGINT,
    exchange            VARCHAR(10)  NOT NULL DEFAULT 'NSE',
    scrape_date         DATE         NOT NULL DEFAULT CURRENT_DATE,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT short_unique UNIQUE (deal_date, symbol)
);

CREATE INDEX IF NOT EXISTS idx_short_date      ON short_deals (deal_date);
CREATE INDEX IF NOT EXISTS idx_short_symbol    ON short_deals (symbol);
CREATE INDEX IF NOT EXISTS idx_short_scrape_dt ON short_deals (scrape_date);

-- ─────────────────────────────────────────────
-- Scrape Run Log
-- Audit trail for every pipeline execution
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scrape_run_log (
    id              BIGSERIAL PRIMARY KEY,
    run_id          UUID         NOT NULL DEFAULT gen_random_uuid(),
    dataset         VARCHAR(50)  NOT NULL,
    status          VARCHAR(20)  NOT NULL,   -- 'success' | 'partial' | 'failed'
    records_fetched INTEGER      DEFAULT 0,
    records_upserted INTEGER     DEFAULT 0,
    error_message   TEXT,
    start_time      TIMESTAMPTZ  NOT NULL,
    end_time        TIMESTAMPTZ,
    scrape_date     DATE         NOT NULL DEFAULT CURRENT_DATE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
