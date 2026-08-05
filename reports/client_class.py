"""
Client classification for NSE bulk and block deals.

The deal feeds carry a free-text `client_name` and nothing else — no client
type, no PAN, no category. Every classification here is therefore an inference
from a name string, and the module is built around three facts about that:

  1. **A handful of names carry most of the volume.** Six desks account for over
     40% of all client rows in the history held. Those are pinned by name in
     `_KNOWN`, because getting them right matters far more than any pattern.

  2. **Some distinctions are genuinely not in the name.** "SETU SECURITIES PVT
     LTD" is a broker and "QE SECURITIES LLP" is a proprietary desk; nothing in
     either string says so. Where a pattern cannot separate them the rule
     assigns the broader bucket rather than guessing, and `confidence()` reports
     how the call was made so a reader can discount it.

  3. **Order is the whole design.** "FRANKLIN TEMPLETON MUTUAL FUND" is an
     Indian mutual fund that happens to carry a foreign brand; "TEMPLETON
     EMERGING MARKETS FUND" is an FPI. The only thing separating them is that
     the domestic rule is tested first. Reordering these rules changes results.

Rules were derived against all 1,280 distinct client names in the deal history,
not invented — see `tests/test_client_class.py` for the cases that pin them.
"""

from __future__ import annotations

import re

# ── Classes, in the order they should be presented ───────────────────────────
# Institutional money first, then intermediaries, then corporates and
# individuals. This is also the section order in the report.
FII    = "FII"
DII    = "DII/MF"
AIF    = "AIF"
HFT    = "HFT"
PROP   = "PROP"
BRKR   = "BRKR"
CORP   = "CORP"
STRAT  = "STRAT"
TRUST  = "TRUST"
HNI    = "HNI"

CLASS_ORDER = [FII, DII, AIF, HFT, PROP, BRKR, CORP, STRAT, TRUST, HNI]

CLASS_LABEL = {
    FII:   "Foreign portfolio investors",
    DII:   "Domestic institutions &mdash; mutual funds, insurers, pension",
    AIF:   "Alternative investment funds",
    HFT:   "High-frequency and quantitative desks",
    PROP:  "Proprietary trading desks",
    BRKR:  "Brokers and intermediaries",
    CORP:  "Corporates and operating companies",
    STRAT: "Strategic holders and promoters",
    TRUST: "Trusts, family offices and foundations",
    HNI:   "Individuals and HUFs",
}

CLASS_BLURB = {
    FII:   "Registered foreign portfolio investors, offshore funds and ODI holders.",
    DII:   "Indian mutual funds, life and general insurers, and pension money.",
    AIF:   "SEBI alternative investment funds, including IFSC-domiciled vehicles.",
    HFT:   "Market-making and quantitative desks trading their own capital at high turnover.",
    PROP:  "Firms trading their own book, distinguishable from brokers only by name knowledge.",
    BRKR:  "Registered brokers. A broker row may be a client trade routed under the broker's name.",
    CORP:  "Operating companies, holding companies and treasuries.",
    STRAT: "Promoters, promoter groups and estates &mdash; holders with a strategic interest.",
    TRUST: "Private trusts, family offices, foundations and employee welfare vehicles.",
    HNI:   "Named individuals and Hindu Undivided Families.",
}


# ── Normalisation ────────────────────────────────────────────────────────────

_PUNCT = re.compile(r"[^\w&]+")
_WS = re.compile(r"\s+")


def normalise(name: str) -> str:
    """Upper-case, punctuation to spaces, whitespace collapsed, space-padded.

    Padding both ends lets every pattern below use a leading and trailing space
    as a cheap word boundary without worrying about string ends.

    Real feed names carry double spaces ("R G  FAMILY TRUST"), trailing dots
    ("CANARA ROBECO MUTUAL FUND."), inline hashes ("S I INVESTMENTS ## BROKING
    PVT.LTD") and mojibake ("FIDELITY FUNDS �� INDIA FOCUS FUND"). All
    of those defeated the previous substring matching.
    """
    if not name:
        return " "
    s = _PUNCT.sub(" ", name.upper())
    return " " + _WS.sub(" ", s).strip() + " "


# ── Pinned names ─────────────────────────────────────────────────────────────
# Substring-matched against the normalised name. These are the desks whose
# classification cannot be derived from the name and whose volume makes a wrong
# call expensive. Ordered dict semantics do not matter — first hit wins by
# iteration, and no key here is a substring of another.
_KNOWN: list[tuple[str, str]] = [
    # High-frequency / quantitative. Several trade under a "SECURITIES" or
    # "RESEARCH" name that would otherwise read as a broker.
    ("HRTI", HFT),                      # Hudson River Trading India
    ("MICROCURVES", HFT),
    ("JUMP TRADING", HFT),
    ("GRAVITON RESEARCH", HFT),
    ("SUSQUEHANNA", HFT),
    ("ALPHAGREP", HFT),
    ("IMC INDIA", HFT),
    ("IMC FINANCIAL", HFT),
    ("IRAGE", HFT),                     # iRage Capital
    ("PLUTUS RESEARCH", HFT),
    ("OPTIVER", HFT),
    ("VIRTU", HFT),
    ("CITADEL SECURITIES", HFT),
    ("TWO SIGMA", HFT),
    ("JANE STREET", HFT),
    ("TOWER RESEARCH", HFT),
    ("XTX MARKETS", HFT),
    ("HUDSON RIVER", HFT),
    ("QUBE RESEARCH", HFT),
    ("SQUAREPOINT", HFT),
    ("DE SHAW", HFT),
    ("QUADEYE", HFT),
    ("DOLAT", HFT),
    ("ESTEE ADVISORS", HFT),
    # Proprietary desks. Only firms known to trade their own book are pinned.
    # Names that merely *look* like a prop desk ("ACME CAPITAL MARKET LIMITED",
    # "HI KLASS TRADING & INVESTMENT LIMITED", "VISTAAR TRADING SERVICE") are
    # deliberately absent: nothing in those strings distinguishes a prop desk
    # from an investment company, and pinning a guess here is worse than letting
    # the pattern rules assign the broader bucket.
    ("JUNOMONETA", PROP),
    ("NK SECURITIES RESEARCH", PROP),
    ("QE SECURITIES", PROP),
    ("SILVERLEAF CAPITAL", PROP),
    ("YUGA STOCKS", PROP),
    ("BHANA EQUITY", PROP),
    # Brokers.
    ("ARIHANT CAPITAL", BRKR),
    ("KOTAK SECURITIES", BRKR),
    ("ZERODHA", BRKR),
    ("ANGEL ONE", BRKR),
    ("SHAREKHAN", BRKR),
    ("MOTILAL OSWAL SEC", BRKR),
    ("AXIS SECURITIES", BRKR),
    ("ICICI SECURITIES", BRKR),
    ("HDFC SECURITIES", BRKR),
    ("EDELWEISS SECURITIES", BRKR),
    ("ANTIQUE STOCK", BRKR),
    ("PRABHUDAS LILLADHER", BRKR),
    ("NIRMAL BANG", BRKR),
    ("MANSUKH SECURITIES", BRKR),
    ("MARWADI", BRKR),
    ("SHARE INDIA SECURITIES", BRKR),
    ("ALACRITY SECURITIES", BRKR),
    ("BONANZA PORTFOLIO", BRKR),
    ("MASTER CAPITAL", BRKR),
    ("ANAND RATHI", BRKR),
    ("GEOJIT", BRKR),
    ("IIFL SECURITIES", BRKR),
    ("5PAISA", BRKR),
    ("UPSTOX", BRKR),
    ("SMC GLOBAL", BRKR),
    ("VENTURA SECURITIES", BRKR),
    # Foreign institutions whose names lack a jurisdiction marker.
    ("SMALLCAP WORLD FUND", FII),
    ("GOVERNMENT OF SINGAPORE", FII),   # GIC
    ("ISHARES", FII),
    ("VANGUARD", FII),
    ("BLACKROCK", FII),
    ("WASATCH", FII),
    ("MATTHEWS", FII),
    ("POLUNIN", FII),
    ("GQG", FII),
    ("TCW", FII),
    ("MOBIUS", FII),
    ("GHISALLO", FII),
    ("RIBBIT", FII),
    ("EIGHT ROADS", FII),
    ("ALPHA WAVE", FII),
    ("PINE OAK", FII),
    ("MCP EMERGING", FII),
    ("DENDANA", FII),
    ("FIH MAURITIUS", FII),
    ("BOFA SECURITIES", FII),
    ("MERRILL LYNCH", FII),
    ("MTBJ", FII),                      # Mitsubishi UFJ Trust, holds Japan GPIF
    ("MUTB", FII),
    ("TNTBC", FII),                     # Nomura Trust & Banking
    ("SEI TRUST", FII),
    ("SUNAMERICA", FII),
    ("OMNIS", FII),
    ("ST JAMES", FII),
    ("MANULIFE", FII),
    ("SAMSUNG", FII),
    ("SVF ", FII),                      # SoftBank Vision Fund vehicles
    # INVESTCORP is deliberately NOT pinned. It is a sponsor brand, not a
    # vehicle type: "INVESTCORP INDIA WAREHOUSING IFSC TRUST" is a GIFT City
    # vehicle and belongs in AIF, while an Investcorp Mauritius entity belongs
    # in FII. Pinning the brand would override the structural marker that
    # actually decides, so the patterns are left to it.
    ("APAX", FII),
    ("CARLYLE", FII),
    ("WARBURG", FII),
    ("BLACKSTONE", FII),
    ("GENERAL ATLANTIC", FII),
    ("SEQUOIA", FII),
    ("PEAK XV", FII),
    ("PROSUS", FII),
    ("NASPERS", FII),
    ("SAIF ", FII),
    ("VIRIDIAN", FII),
]

# Indian asset managers and insurers. Tested before the foreign rules so a
# domestic fund carrying a global brand is not read as an FPI.
_INDIAN_AMC = (
    "SBI|HDFC|ICICI|KOTAK|AXIS|NIPPON INDIA|ADITYA BIRLA|UTI|DSP|MIRAE|TATA|"
    "INVESCO|CANARA ROBECO|EDELWEISS|WHITEOAK CAPITAL|BANDHAN|QUANT|HELIOS|ITI|"
    "MOTILAL OSWAL|360 ONE|SUNDARAM|BAJAJ|MAHINDRA MANULIFE|TRUST|HSBC|"
    "BANK OF INDIA|LIC|BARODA|UNION|GROWW|ZERODHA|NAVI|PPFAS|PARAG PARIKH|"
    "JM FINANCIAL|SHRIRAM|TAURUS|QUANTUM|OLD BRIDGE|SAMCO|UNIFI|WHITE OAK"
)


# ── Ordered rules ────────────────────────────────────────────────────────────
# (class, compiled pattern). First match wins. Every pattern is written against
# the normalised, space-padded form.

def _rx(*alts: str) -> re.Pattern:
    return re.compile("|".join(alts))


_RULES: list[tuple[str, re.Pattern]] = [
    # ---- Domestic institutions. FIRST, so an Indian fund with a foreign brand
    # ---- ("FRANKLIN TEMPLETON MUTUAL FUND") is not read as an FPI.
    (DII, _rx(
        r" MUTUAL FUND",
        r" LIFE INSURANCE ", r" GENERAL INSURANCE ", r" HEALTH INSURANCE ",
        r" INSURANCE COMPANY ", r" INSURANCE CO ",
        r" PENSION SYSTEM ", r" NPS ", r" EPFO ", r" PROVIDENT FUND ORGANISATION ",
        rf" ({_INDIAN_AMC}) (ASSET MANAGEMENT|AMC|MF) ",
        r" LIC OF INDIA ",
    )),

    # ---- Alternative investment funds. Before FII because an IFSC or
    # ---- scheme-numbered vehicle is an AIF even when it reads foreign.
    (AIF, _rx(
        r" AIF ", r" AIF$", r"AIF ",
        r" ALTERNATIVE INVESTMENT",
        r" CATEGORY I+ ",
        r" IFSC ",
        r" SCHEME [IVX0-9]", r" SERIES [IVX0-9]",
        r" REAL ESTATE FUND", r" COMMERCIAL REF ", r" REF IFSC ",
        r" CROSSOVER OPPORTUNITIES FUND", r" TRUE NORTH FUND",
        r" PI OPPORTUNITIES",
    )),

    # ---- Foreign portfolio investors.
    (FII, _rx(
        # Explicit regulatory markers.
        r" FPI ", r" ODI ", r" FOREIGN PORTFOLIO", r" PARTICIPATORY NOTE",
        r" ODI HOLDER",
        # Jurisdictions and foreign vehicle forms. Note the absence of "GLOBAL":
        # RAJASTHAN GLOBAL SECURITIES and TRANSGLOBAL SECURITIES are Indian
        # brokers, so GLOBAL alone is not evidence of anything.
        r" MAURITIUS", r" CAYMAN", r" LUXEMBOURG", r" IRELAND", r" CYPRUS",
        r" NETHERLANDS", r" SINGAPORE ", r" PTE ", r" PTY ", r" PCC ",
        r" SICAV", r" ICAV", r" PLC ", r" LLC ", r" INC ", r" NV ", r" SE ",
        r" OFFSHORE",
        # Fund shapes that only foreign vehicles use.
        r" EMERGING MARKET", r" MASTER FUND", r" MOTHER FUND", r" ETF ",
        r" CIT ", r" UNIT TRUST", r" INVESTMENT TRUST ", r" COMMINGLED",
        r" TRUSTEE OBO", r" AS TRST FOR", r" AS THE TRUSTEE OF",
        r" DEVELOPING MARKETS", r" INTERNATIONAL FUND", r" FUNDS INDIA",
        # Sovereign and pension money.
        r" GOVERNMENT OF ", r" SOVEREIGN", r" PENSION INV", r" GPIF ",
        # Global banks and managers.
        r" GOLDMAN SACHS", r" MORGAN STANLEY", r" JP MORGAN", r" JPMORGAN",
        r" CITIGROUP", r" CITIBANK", r" BNP PARIBAS", r" SOCIETE GENERALE",
        r" DEUTSCHE BANK", r" BARCLAYS", r" NOMURA", r" UBS ", r" HSBC BANK",
        r" FIDELITY", r" FMRC", r" FIAM", r" TEMPLETON", r" FRANKLIN",
        r" ABERDEEN", r" SCHRODERS", r" LAZARD", r" ASHOKA WHITEOAK",
        r" CRAFT EM", r" WHITEOAK EMERGING",
    )),

    # ---- Quantitative / high-frequency. Pattern tail after the pinned list.
    # QUANT is safe this far down: "QUANT MUTUAL FUND" is an Indian AMC and the
    # domestic rule above has already claimed it.
    (HFT, _rx(
        # No leading space: the marker is frequently welded into a coined name
        # ("BLITZQUANT", "ALGOQUANT", "MATHISYS QUANTCAP").
        r"QUANT", r"ALGO", r" HFT ", r" ARBITRAGE",
        r" RESEARCH CAPITAL", r" MARKET MAKING",
    )),

    # ---- Proprietary desks.
    (PROP, _rx(
        r" SECURITIES RESEARCH", r" FINSOL", r" PROP ", r" PROPRIETARY",
        r" TRADING STRATEGIES", r" TRADE TECH",
    )),

    # ---- Brokers and intermediaries. Broad on purpose: BROK covers BROKING,
    # ---- BROKER and BROKERS, which the previous rules missed by requiring
    # ---- "BROKING LTD" or "BROKING PVT" exactly.
    (BRKR, _rx(
        r" BROK",
        r" STOCK BROKING", r" SHARE BROKING", r" SHARES AND STOCK",
        r" INTERMEDIARIES", r" DEPOSITORY",
        r" E TRADE SECURITIES",
        r" COMMODITY BROK",
        # Broad catch-all, last within this rule. An unidentified "X SECURITIES"
        # or "X SHARES" is far more likely a registered broker than anything
        # else, and BRKR is the honest bucket for it — the alternative is HNI,
        # which asserts it is a private individual.
        r" SECURITIES ", r" SHARES ", r" STOCKS ",
        r" COMMODITIES (LTD|LIMITED|PVT|PRIVATE)",
    )),

    # ---- Strategic holders. Before TRUST so an estate is not read as a trust.
    (STRAT, _rx(
        r" ESTATE OF ", r" PROMOTER", r" STRATEGIC INVESTOR",
        r" JHUNJHUNWALA",
    )),

    # ---- Trusts, family offices, foundations. After FII/AIF, both of which
    # ---- legitimately use the word TRUST in a fund name.
    (TRUST, _rx(
        r" FAMILY TRUST", r" FAMILY PRIVATE TRUST", r" FAMILY OFFICE",
        r" FOUNDATION", r" ENDOWMENT", r" CHARITABLE",
        r" WELFARE TRUST", r" BENEFICIARY TRUST", r" EMPLOYEE.* TRUST",
        r" TRUST $",
    )),

    # ---- Individuals and HUFs. Tested before CORP so "RAKESH KUMAR UPPAL AND
    # ---- SONS HUF" is not caught by a corporate suffix.
    (HNI, _rx(
        r" HUF ", r" HUF$", r"HUF ", r" AND SONS ", r" & SONS ",
    )),
]

# A corporate suffix is what separates an operating company from an individual.
_CORP_SUFFIX = _rx(
    r" (LTD|LIMITED|LLP|LLC|INC|PLC|PVT|PRIVATE|COMPANY|CORP|CORPORATION|"
    r"ENTERPRISE|ENTERPRISES|SERVICES|SOLUTIONS|VENTURES|VENTURE|HOLDINGS|"
    r"INVESTMENTS|INVESTMENT|ASSOCIATES|PARTNERS|INDUSTRIES|TRADERS|TRADING|"
    r"TRADELINK|TRADEFIN|REALTORS|PACKAGING|ECOMMERCE|FINCON|FINTECH|"
    r"TECHNOLOGIES|TECHNOLOGY|INTERNATIONAL|EXPORTS|IMPORTS|AGENCIES|"
    r"DISTRIBUTORS|MARKETING|CAPITAL|FINANCE|FINSERV|BANK|FUND|TRUST) "
)


def classify(name: str) -> str:
    """The client class for a raw feed name. Never raises; never returns None."""
    n = normalise(name)
    if n.strip() == "":
        return CORP

    for needle, tag in _KNOWN:
        if needle in n:
            return tag

    for tag, rx in _RULES:
        if rx.search(n):
            return tag

    return CORP if _CORP_SUFFIX.search(n) else HNI


def confidence(name: str) -> str:
    """How the call was made: 'pinned', 'pattern' or 'fallback'.

    The report surfaces this nowhere, but it is what makes a misclassification
    debuggable: a wrong 'pinned' is a bad entry in _KNOWN, a wrong 'pattern' is
    a bad rule, and a wrong 'fallback' means the name carries no signal at all.
    """
    n = normalise(name)
    if any(k in n for k in (k for k, _ in _KNOWN)):
        return "pinned"
    if any(rx.search(n) for _, rx in _RULES):
        return "pattern"
    return "fallback"
