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


# ── Non-English corporate forms ──────────────────────────────────────────────
# REGRESSION: every name in this block was classified HNI — "Individuals and
# HUFs" — because the corporate-suffix list was entirely Anglophone. It was
# found by the weekly report, where these aggregate into a class net-flow
# headline: on the week of 17-21 Aug 2026, RESILIENT ASSET MANAGEMENT B V alone
# was 101% of the reported HNI net, so the report stated that individuals sold
# ₹5,819 cr net when the number was one Dutch holding vehicle.

@pytest.mark.parametrize("name", [
    "RESILIENT ASSET MANAGEMENT B V",       # the Antfin vehicle that held Paytm
    "CREDITACCESS INDIA B.V.",              # feed writes B.V.; normalise -> " B V "
    "TENCENT CLOUD EUROPE B.V.",
    "BAYER AG",
    "BAYER CROPSCIENCE AKTIENGESELLSCHAFT",
    "FILA - FABBRICA ITALIANA LAPIS ED AFFINI SPA",
])
def test_foreign_corporate_forms_are_not_individuals(name):
    assert classify(name) == CORP


def test_foreign_corporate_form_is_corp_not_fii():
    """The bucket choice, pinned deliberately.

    These are operating and holding companies, so CORP ("operating companies,
    holding companies and treasuries") is accurate and FII ("foreign portfolio
    investors") is not. Routing every foreign suffix to FII would swap one wrong
    answer for a subtler one that is harder to notice.
    """
    assert classify("BAYER AG") == CORP
    assert classify("TENCENT CLOUD EUROPE B.V.") == CORP


def test_a_suffix_match_is_a_pattern_call_not_a_fallback():
    """The query that found the bug has to keep working.

    'fallback' must mean the name carries no signal, because querying for
    fallback-classified names is how these were found. A corporate suffix is
    signal; a bare personal name is not.
    """
    assert confidence("BAYER AG") == "pattern"
    assert confidence("RESILIENT ASSET MANAGEMENT B V") == "pattern"
    assert confidence("VISHAL MAHESH WAGHELA") == "fallback"


# ── Foreign fund vehicles ────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "NORTH ROCK SG VCC",
    "NRSGVCC",                              # ORDER: welded, no word boundary
    "NECTA BLOOM VCC - NECTA BLOOM ONE",
    "ACM GLOBAL FUND VCC",
    "GSS OPPORTUNITIES INVESTMENT I VCC",
    "CULLINAN OPPRTS FUND VCC-CULLINAN OPPORTUNITIES INCORPORATED VCC SUB FUND 1",
])
def test_singapore_vcc_is_a_foreign_fund(name):
    """Only funds use the VCC form, so unlike a corporate suffix it is evidence
    of a portfolio investor. Several of these previously read as CORP."""
    assert classify(name) == FII


def test_vcc_beats_the_quant_pattern():
    """ORDER: the FII rule must be tested before the HFT one.

    "VISTA AXIS VCC-QUANT FUND" is a Singapore fund, not a quant prop desk, but
    the HFT rule matches a bare "QUANT" anywhere in the name.
    """
    assert classify("VISTA AXIS VCC-QUANT FUND") == FII


@pytest.mark.parametrize("name", [
    "ALLIANZ GLOBAL INVESTORS GMBH ACTING ON BEHALF OF ALLIANZ EEE FONDS",
    "METZLER ASSET MANAGEMENT GMBH FOR MI-FONDS 415",
    "BAYERNINVEST KVG MBH ON BEHALF OF ERI BAYERNINVEST FONDS AKTIEN ASIEN",
    "APT-UNIVERSAL-FONDS",
])
def test_german_fonds_is_a_foreign_fund(name):
    """FONDS, not GMBH, is what says the money is a fund.

    The GmbH is only the manager's corporate form — "ALLIANZ GLOBAL INVESTORS
    GMBH" is the vehicle through which the fund trades, and the fund is what is
    being classified.
    """
    assert classify(name) == FII


def test_university_endowment_is_a_foreign_investor():
    assert classify("UNIVERSITY OF NOTRE DAME DU LAC") == FII


def test_endowment_alone_stays_with_trust():
    """ORDER: UNIVERSITY went to FII; ENDOWMENT deliberately did not.

    A domestic endowment is not an FPI, and only the university form is
    reliably offshore in this feed.
    """
    assert classify("SOME CHARITABLE ENDOWMENT") == TRUST


# ── Fund-series shapes ───────────────────────────────────────────────────────

def test_trailing_roman_numeral_fund_series_is_an_aif():
    """The SCHEME/SERIES patterns miss a numeral that trails the name directly."""
    assert classify("MADISON INDIA OPPORTUNITIES IV") == AIF


@pytest.mark.parametrize("name", [
    "BUOYANT OPPORTUNITIES STRATEGY",
    "BUOYANT OPPORTUNITIES STRATEGY - II",
    "BUOYANT OPPORTUNITIES STRATEGY-III",
    "KOTAK PERFORMING RE CREDIT STRATEGY FUND-I",
])
def test_pms_strategy_vehicles_are_aifs(name):
    assert classify(name) == AIF


def test_strategy_rule_does_not_steal_offshore_pcc_funds():
    """ORDER + REGRESSION: why the rule is not a bare " STRATEGY ".

    The AIF rule runs before the FII rule, so a bare " STRATEGY " would move
    this Gulf PCC fund from FII to AIF. It is narrowed to " OPPORTUNITIES
    STRATEGY" and " STRATEGY FUND" precisely to leave this one alone.
    """
    assert classify("AL MAHA INVESTMENT FUND PCC - ONYX STRATEGY") == FII


def test_named_individuals_are_still_individuals():
    """The counterweight: none of the above may start eating real people.

    All five are genuine individual holders from the deal history, and HNI is
    the correct answer for each.
    """
    for name in (
        "SURENDERPAL SINGH SALUJA", "ARUNA GANESH", "JAYANTI SINHA",
        "VISHAL MAHESH WAGHELA", "ADITYA KUMAR HALWASIYA",
    ):
        assert classify(name) == HNI, name
