"""
Cases pinning reports/client_class.py.

Every name here is a real client name from the deal history, not an invention.
The ones marked ORDER or REGRESSION are the ones that were wrong before and will
silently break again if the rule order changes, so they are the reason this file
exists.

Run: python -m pytest tests/test_client_class.py -q
"""

import pytest

from reports.client_class import (
    AIF, BRKR, CORP, DII, FII, HFT, HNI, PROP, STRAT, TRUST,
    classify, confidence, normalise,
)


# ── Normalisation ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expect_contains", [
    ("R G  FAMILY TRUST", " FAMILY TRUST "),          # double space in the feed
    ("CANARA ROBECO MUTUAL FUND.", " MUTUAL FUND "),  # trailing dot
    ("S I INVESTMENTS ## BROKING PVT.LTD", " BROKING PVT LTD "),
    ("HDFC LIFE INSURANCE CO. LTD.", " LIFE INSURANCE CO LTD "),
    ("RAVI GOYAL (HUF)", " HUF "),
    ("MANSUKH SECURITIES & FINANCE LIMITED", "&"),    # ampersand is kept
])
def test_normalise_repairs_feed_noise(raw, expect_contains):
    assert expect_contains in normalise(raw)


def test_normalise_never_raises_on_empty():
    assert normalise("") == " "
    assert normalise(None) == " "


# ── Order-dependent cases: the whole design ──────────────────────────────────

def test_indian_mutual_fund_with_foreign_brand_is_domestic():
    """ORDER: the domestic rule must run before the foreign one.

    Both names carry the Templeton brand. Only the word order and the trailing
    "MUTUAL FUND" separate an Indian AMC from an offshore fund.
    """
    assert classify("FRANKLIN TEMPLETON MUTUAL FUND") == DII
    assert classify("TEMPLETON EMERGING MARKETS FUND") == FII


def test_whiteoak_split_by_vehicle():
    assert classify("WHITEOAK CAPITAL MUTUAL FUND") == DII
    assert classify("ASHOKA WHITEOAK EMERGING MARKETS EQUITY EX CHINA FUND") == FII


def test_quant_mutual_fund_is_not_a_quant_desk():
    """ORDER: ' QUANT' is an HFT marker, but DII is tested first."""
    assert classify("QUANT MUTUAL FUND") == DII
    assert classify("MATHISYS QUANTCAP LLP") == HFT


def test_ifsc_vehicle_is_aif_not_fii():
    """ORDER: AIF precedes FII so an IFSC fund is not read as offshore."""
    assert classify("KOTAK REAL ESTATE FUND X IFSC") == AIF
    assert classify("INVESTCORP INDIA WAREHOUSING IFSC TRUST") == AIF


def test_estate_is_strategic_not_trust():
    assert classify("ESTATE OF LATE MR. RAKESH JHUNJHUNWALA") == STRAT


def test_huf_beats_corporate_suffix():
    """ORDER: HNI precedes the CORP fallback, so 'AND SONS' is not a company."""
    assert classify("RAKESH KUMAR UPPAL AND SONS HUF") == HNI
    assert classify("ARUN KOCHAR & SONS (HUF)") == HNI
    assert classify("RATHOD MANOJ CHHAGANLAL HUF") == HNI


# ── Regressions: every one of these was misclassified before ─────────────────

@pytest.mark.parametrize("name,expect", [
    # REGRESSION: fell to CORP because the broker patterns demanded
    # "BROKING LTD" or "BROKING PVT" exactly.
    ("NEO APEX SHARE BROKING SERVICES LLP", BRKR),
    ("MANSI SHARE AND STOCK BROKING PRIVATE LIMITED", BRKR),
    ("MARWADI CHANDARANA INTERMEDIARIES BROKERS PRIVATE LIMITED", BRKR),
    ("JIAUM BROKING LLP", BRKR),
    ("PARTH INFIN BROKERS PVT LTD", BRKR),
    ("SETU SECURITIES PVT LTD", BRKR),
    ("MSB E TRADE SECURITIES LIMITED", BRKR),
    ("ORION STOCKS LTD", BRKR),
    ("ARIHANT CAPITAL MARKETS LIMITED", BRKR),
    # REGRESSION: known quant desks trading under a "SECURITIES" or "RESEARCH"
    # name, all previously CORP.
    ("ALPHAGREP SECURITIES PRIVATE LIMITED", HFT),
    ("IMC INDIA SECURITIES PRIVATE LIMITED", HFT),
    ("IRAGE BROKING SERVICES LLP", HFT),
    ("BLITZQUANT RESEARCH LLP", HFT),
    ("HRTI PRIVATE LIMITED", HFT),
    ("MICROCURVES TRADING PRIVATE LIMITED", HFT),
    ("GRAVITON RESEARCH CAPITAL LLP", HFT),
    # REGRESSION: prop desks, previously CORP.
    ("QE SECURITIES LLP", PROP),
    ("JUNOMONETA FINSOL PRIVATE LIMITED", PROP),
    ("NK SECURITIES RESEARCH PRIVATE LIMITED", PROP),
    ("SILVERLEAF CAPITAL SERVICES PRIVATE LIMITED", PROP),
    # REGRESSION: foreign money with no jurisdiction word in the name.
    ("THE MTBJ LTD. AS TRST FOR GOVRNMNT PENSION INVSTMNT FUND MUTB400045794", FII),
    ("SMALLCAP WORLD FUND INC", FII),
    ("GHISALLO MASTER FUND LP", FII),
    ("GOVERNMENT OF SINGAPORE", FII),
    ("ISHARES CORE MSCI EMERGING MARKETS ETF", FII),
    ("INTEGRATED CORE STRATEGIES ASIA PTE LTD", FII),
    ("FIH MAURITIUS INVESTMENTS LTD", FII),
    ("EIGHT ROADS INVESTMENTS MAURITIUS II LIMITED", FII),
    ("BNP PARIBAS ARBITRAGE - ODI", FII),
    # REGRESSION: trusts that landed in HNI.
    ("R G FAMILY  TRUST", TRUST),
    ("PFL EMPLOYEE WELFARE TRUST", TRUST),
    ("VIDEEP KABRA BENEFICIARY TRUST", TRUST),
    ("MOTILAL OSWAL FOUNDATION", TRUST),
    # REGRESSION: firms that landed in HNI for want of a corporate suffix.
    ("SHRENI SHARES PVT", BRKR),
    ("KIFS  ENTERPRISE", CORP),
    ("LAXMI TRADE SOLUTIONS", CORP),
])
def test_regressions(name, expect):
    assert classify(name) == expect, f"{name!r} -> {classify(name)}, wanted {expect}"


# ── Things that must NOT be over-matched ─────────────────────────────────────

@pytest.mark.parametrize("name", [
    "RAJASTHAN GLOBAL SECURITIES PVT LTD",
    "TRANSGLOBAL SECURITIES LTD",
    "PACE COMMODITY BROKERS PRIVATE LIMITED",
])
def test_global_is_not_a_foreign_marker(name):
    """'GLOBAL' appears in Indian broker names, so it is deliberately not an
    FII pattern. These are the names that would break if it were added."""
    assert classify(name) == BRKR


@pytest.mark.parametrize("name", [
    "VISHAL MAHESH WAGHELA",
    "JAGID VANITABEN RAJENDRAPRASAD",
    "AMIT KUMAR JAIN",
    "THAKOR NAYANA CHANDUBHAI",
])
def test_plain_individuals_stay_hni(name):
    assert classify(name) == HNI


@pytest.mark.parametrize("name", [
    "CHAUBARA EATS PRIVATE LIMITED",
    "RAMDOOT REALTORS PVT LTD",
    "PLASTOMATIC PACKAGING PRIVATE LIMITED",
    "BHAVISHYA ECOMMERCE PRIVATE LIMITED",
    "L7 HITECH PRIVATE LIMITED",
])
def test_operating_companies_stay_corp(name):
    assert classify(name) == CORP


# ── Robustness ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["", None, "   ", "###", "12345"])
def test_never_raises(bad):
    assert classify(bad) in {
        FII, DII, AIF, HFT, PROP, BRKR, CORP, STRAT, TRUST, HNI
    }


def test_confidence_reports_how_the_call_was_made():
    assert confidence("HRTI PRIVATE LIMITED") == "pinned"
    assert confidence("SOME RANDOM BROKING LLP") == "pattern"
    assert confidence("VISHAL MAHESH WAGHELA") == "fallback"
