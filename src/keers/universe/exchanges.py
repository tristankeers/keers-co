"""The exchanges in the global universe, and how each one's listings are named and priced.

``code`` is the Bloomberg exchange code that OpenFIGI uses. ``mic`` is the ISO 10383 market identifier. The
currency is the exchange's usual trading currency: it is a default, not a fact about any one listing (London,
for example, has shares quoted in pence, dollars and euros). A listing keeps ``currency_basis="exchange-default"``
until a Yahoo check confirms or corrects it.

``symbol_confidence`` says how far the Yahoo symbol worked out from the OpenFIGI ticker can be trusted:
- ``high``: the codes are the same on both (numeric codes in Tokyo, Seoul, Taipei, Shanghai, Shenzhen, Hong Kong).
- ``medium``: usually the same, with known exceptions (share classes, punctuation).
- ``low``: Yahoo uses a different code system (Malaysia uses numeric codes; Bloomberg uses letters).

``hub`` marks an exchange that also lists many companies whose home market is elsewhere (Xetra lists thousands of
US and Asian shares; Mexico's SIC lists foreign shares). When the same share class trades in several places, the
listing on a non-hub exchange is kept as the company's primary listing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Exchange:
    code: str  # Bloomberg / OpenFIGI exchange code
    mic: str
    name: str
    country: str  # ISO 3166-1 alpha-2, the exchange's country
    currency: (
        str  # ISO 4217; GBX, ZAC and ILA are the minor units (pence, cents, agorot) prices are quoted in
    )
    yahoo_suffix: str
    symbol_confidence: str = "medium"
    hub: bool = False
    security_types: tuple[str, ...] = ("Common Stock",)
    pad: int = 0  # left-pad numeric tickers to this width for Yahoo (Hong Kong: 700 -> 0700)
    class_dash: bool = (
        False  # Yahoo writes share classes with a dash where Bloomberg runs them on (VOLVB -> VOLV-B)
    )


# US listings come from SEC and Nasdaq Trader; OpenFIGI's US composite only adds FIGIs to them.
US = Exchange("US", "", "United States (composite)", "US", "USD", "", "high",
              security_types=("Common Stock", "Depositary Receipt"))  # fmt: skip

# Order matters: when a share class trades on several non-hub exchanges, the earlier one is taken as primary.
NON_US: tuple[Exchange, ...] = (
    # Europe
    Exchange("LN", "XLON", "London Stock Exchange", "GB", "GBX", ".L"),
    Exchange("FP", "XPAR", "Euronext Paris", "FR", "EUR", ".PA"),
    Exchange("NA", "XAMS", "Euronext Amsterdam", "NL", "EUR", ".AS"),
    Exchange("BB", "XBRU", "Euronext Brussels", "BE", "EUR", ".BR"),
    Exchange("PL", "XLIS", "Euronext Lisbon", "PT", "EUR", ".LS"),
    Exchange("ID", "XDUB", "Euronext Dublin", "IE", "EUR", ".IR"),
    Exchange("IM", "XMIL", "Borsa Italiana", "IT", "EUR", ".MI"),
    Exchange("SM", "XMAD", "Bolsa de Madrid", "ES", "EUR", ".MC"),
    Exchange("SW", "XSWX", "SIX Swiss Exchange", "CH", "CHF", ".SW"),
    Exchange("AV", "XWBO", "Wiener Börse", "AT", "EUR", ".VI"),
    Exchange("SS", "XSTO", "Nasdaq Stockholm", "SE", "SEK", ".ST", class_dash=True),
    Exchange("NO", "XOSL", "Oslo Børs", "NO", "NOK", ".OL", class_dash=True),
    Exchange("DC", "XCSE", "Nasdaq Copenhagen", "DK", "DKK", ".CO", class_dash=True),
    Exchange("FH", "XHEL", "Nasdaq Helsinki", "FI", "EUR", ".HE", class_dash=True),
    Exchange("PW", "XWAR", "Warsaw Stock Exchange", "PL", "PLN", ".WA"),
    Exchange("GA", "ASEX", "Athens Stock Exchange", "GR", "EUR", ".AT"),
    Exchange("TI", "XIST", "Borsa Istanbul", "TR", "TRY", ".IS"),
    Exchange("GY", "XETR", "Xetra", "DE", "EUR", ".DE", hub=True),
    # Middle East and Africa
    Exchange("IT", "XTAE", "Tel Aviv Stock Exchange", "IL", "ILA", ".TA"),
    Exchange("AB", "XSAU", "Saudi Exchange", "SA", "SAR", ".SR", "high"),
    Exchange("SJ", "XJSE", "Johannesburg Stock Exchange", "ZA", "ZAC", ".JO"),
    # Asia-Pacific
    Exchange("JT", "XTKS", "Tokyo Stock Exchange", "JP", "JPY", ".T", "high"),
    Exchange("HK", "XHKG", "Hong Kong Exchanges", "HK", "HKD", ".HK", "high", pad=4),
    Exchange("CG", "XSHG", "Shanghai Stock Exchange", "CN", "CNY", ".SS", "high"),
    Exchange("CS", "XSHE", "Shenzhen Stock Exchange", "CN", "CNY", ".SZ", "high"),
    Exchange("TT", "XTAI", "Taiwan Stock Exchange", "TW", "TWD", ".TW", "high"),
    Exchange("KS", "XKRX", "Korea Exchange (KOSPI)", "KR", "KRW", ".KS", "high"),
    Exchange("KQ", "XKOS", "Korea Exchange (KOSDAQ)", "KR", "KRW", ".KQ", "high"),
    Exchange("IS", "XNSE", "National Stock Exchange of India", "IN", "INR", ".NS"),
    Exchange("SP", "XSES", "Singapore Exchange", "SG", "SGD", ".SI"),
    Exchange("MK", "XKLS", "Bursa Malaysia", "MY", "MYR", ".KL", "low"),
    Exchange("TB", "XBKK", "Stock Exchange of Thailand", "TH", "THB", ".BK"),
    Exchange("IJ", "XIDX", "Indonesia Stock Exchange", "ID", "IDR", ".JK"),
    Exchange("PM", "XPHS", "Philippine Stock Exchange", "PH", "PHP", ".PS"),
    Exchange("AU", "XASX", "ASX", "AU", "AUD", ".AX"),
    Exchange("NZ", "XNZE", "NZX", "NZ", "NZD", ".NZ"),
    # Americas
    Exchange("CT", "XTSE", "Toronto Stock Exchange", "CA", "CAD", ".TO"),
    Exchange("CV", "XTSX", "TSX Venture Exchange", "CA", "CAD", ".V"),
    Exchange("BZ", "BVMF", "B3 (Brazil)", "BR", "BRL", ".SA"),
    Exchange("MM", "XMEX", "Bolsa Mexicana de Valores", "MX", "MXN", ".MX", hub=True),
    Exchange("CI", "XSGO", "Santiago Stock Exchange", "CL", "CLP", ".SN"),
)

BY_CODE: dict[str, Exchange] = {e.code: e for e in (US, *NON_US)}
RANK: dict[str, int] = {e.code: i for i, e in enumerate((US, *NON_US))}

# US venues, from the Nasdaq Trader symbol directory's exchange letters and SEC's exchange labels.
NASDAQ_TRADER_MIC = {"A": "XASE", "N": "XNYS", "P": "ARCX", "Z": "BATS", "V": "IEXG"}
US_MIC_NAME = {"XNAS": "Nasdaq", "XNYS": "NYSE", "XASE": "NYSE American", "ARCX": "NYSE Arca",
               "BATS": "Cboe BZX", "IEXG": "IEX", "OTCM": "OTC Markets"}  # fmt: skip
SEC_EXCHANGE_MIC = {"nasdaq": "XNAS", "nyse": "XNYS", "otc": "OTCM", "cboe": "BATS"}

# Minor currency units as Yahoo writes them, mapped to the codes stored here.
MINOR_UNITS = {"GBp": "GBX", "GBX": "GBX", "ZAc": "ZAC", "ZAC": "ZAC", "ILA": "ILA", "ILa": "ILA"}


def local_ticker(raw: str) -> str:
    """One spelling of a ticker for keys: share-class separators become a dot (BRK/B, BRK-B, BRK.B -> BRK.B)."""
    t = raw.strip().upper().rstrip("/*")
    return re.sub(r"[/\-. ]+", ".", t)


def yahoo_symbol(ex: Exchange, ticker: str) -> str:
    """Yahoo's symbol for a listing: local ticker with dots as dashes, plus the exchange suffix (VOD.L, 0700.HK)."""
    t = local_ticker(ticker)
    if ex.pad and t.isdigit():
        t = t.zfill(ex.pad)
    return t.replace(".", "-") + ex.yahoo_suffix


def class_dash_variant(ex: Exchange, ticker: str) -> str | None:
    """For Nordic tickers that run the share class on (VOLVB), the dashed Yahoo spelling (VOLV-B.ST)."""
    t = local_ticker(ticker)
    if not ex.class_dash or "." in t or len(t) < 3 or t[-1] not in "ABCDR":
        return None
    return f"{t[:-1]}-{t[-1]}{ex.yahoo_suffix}"
