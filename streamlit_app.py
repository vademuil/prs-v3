"""
Price Recommendation Tool by Rokky
==================================

Streamlit app that, for a given Steam AppID:
  1. Fetches regional prices via the Steam Store API.
  2. Applies inclusive VAT from the Steam tax FAQ.
  3. Subtracts the distributor fee.
  4. Converts everything to USD.
  5. Deduplicates by Steam currency tier (USD is split into
     USD / USD_CIS / USD_SASIA / USD_MENA / USD_LATAM).
  6. Groups currencies into packages (ROW / ASIA / CN / RU-CIS / LATAM / MENA),
     picks a base currency per package, and recommends RAISING publisher USD
     of the other currencies to match the base (raise-only).
  7. Reverses target publisher USD back into retail USD using VAT and the
     distributor fee, then ψ-rounds to .99.

Run:
    pip install -r requirements.txt
    streamlit run streamlit_app.py
"""

from __future__ import annotations

import base64
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from math import floor

import pandas as pd
import requests
import streamlit as st

# ----------------------------------------------------------------------------
# Brand palette
# ----------------------------------------------------------------------------

BRAND = {
    "primary":    "#4600FF",   # purple — buttons, accents
    "bg":         "#FFFFFF",
    "text":       "#1A1A1A",
    "green":      "#3DD070",   # success / OK
    "orange":     "#FF7F42",   # any price change
    "pink":       "#FF3895",   # large gap (>15%)
}
# Translucent tints used as row backgrounds (so text remains readable)
ROW_TINT_ORANGE = "rgba(255, 127, 66, 0.20)"
ROW_TINT_PINK   = "rgba(255, 56, 149, 0.20)"


def inject_css() -> None:
    """
    Load Poppins and apply targeted Streamlit styling.

    Important: do NOT use broad selectors like [class*="st-"] with !important
    on font-family — that overrides Material Symbols Rounded on Streamlit's
    icons (the sidebar collapse arrow renders as raw text 'keyboard_double_…').
    Instead, set Poppins at the root (cascade) and EXPLICITLY restore the
    icon font.
    """
    css = """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700&display=swap');

    /* Poppins via the root — cascades to text elements */
    html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stSidebar"] {
        font-family: 'Poppins', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    }

    /* Restore the Material Symbols font on icon elements so they render as
       glyphs and not as raw text */
    [class*="material-icons"],
    [class*="material-symbols"],
    .material-icons,
    .material-icons-round,
    .material-icons-outlined,
    .material-symbols-rounded,
    .material-symbols-outlined,
    .material-symbols-sharp,
    [data-testid="stIconMaterial"],
    span[style*="Material Symbols"],
    span[style*="material-symbols"] {
        font-family: 'Material Symbols Rounded', 'Material Symbols Outlined',
                     'Material Icons' !important;
    }

    /* Headings */
    h1, h2, h3, h4 {
        font-family: 'Poppins', sans-serif;
        font-weight: 600;
        color: #1A1A1A;
    }

    /* Primary button */
    .stButton > button[kind="primary"],
    .stButton > button[data-testid="baseButton-primary"] {
        background-color: #4600FF !important;
        color: #FFFFFF !important;
        border: none !important;
        border-radius: 8px !important;
        font-weight: 600 !important;
        padding: 10px 24px !important;
        transition: background-color 0.15s ease, box-shadow 0.15s ease;
    }
    .stButton > button[kind="primary"]:hover {
        background-color: #3700CC !important;
        box-shadow: 0 4px 12px rgba(70, 0, 255, 0.25);
    }

    /* Outline-style download buttons */
    .stDownloadButton > button {
        background-color: #FFFFFF !important;
        color: #4600FF !important;
        border: 1.5px solid #4600FF !important;
        border-radius: 8px !important;
        font-weight: 500 !important;
    }
    .stDownloadButton > button:hover {
        background-color: #4600FF !important;
        color: #FFFFFF !important;
    }

    /* Highlight the active tab in the primary color */
    .stTabs [aria-selected="true"] {
        color: #4600FF !important;
    }
    .stTabs [data-baseweb="tab-highlight"] {
        background-color: #4600FF !important;
    }

    /* Colored dots in the legend */
    .legend-dot {
        display: inline-block;
        width: 14px;
        height: 14px;
        border-radius: 3px;
        vertical-align: middle;
        margin-right: 6px;
    }

    /* Lift the main content slightly */
    .block-container {
        padding-top: 2rem;
    }
    </style>
    """
    st.markdown(css, unsafe_allow_html=True)


def render_logo() -> None:
    """Logo in the header. Looked up as logo.svg next to the script."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "logo.svg"),
        os.path.join(here, "Black.svg"),
        "logo.svg",
    ]
    logo_path = next((p for p in candidates if os.path.exists(p)), None)
    if not logo_path:
        return  # graceful: no logo file, no logo rendered

    with open(logo_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")

    # Bigger height + generous padding around so the SVG isn't visually clipped
    # by the parent flexbox / block-container.
    st.markdown(
        f'''
        <div style="display:flex; align-items:center;
                    padding: 24px 0 28px; margin: 0;
                    overflow: visible; line-height: 0;">
            <img src="data:image/svg+xml;base64,{b64}"
                 alt="Rokky"
                 style="height: 110px; width: auto; max-width: 100%;
                        display: block; object-fit: contain; overflow: visible;
                        margin: 0; padding: 0;"/>
        </div>
        ''',
        unsafe_allow_html=True,
    )

# ----------------------------------------------------------------------------
# VAT table (inclusive). Source:
# https://partner.steamgames.com/doc/finance/taxfaq (Current Tax Rates section)
# Snapshot taken May 2026.
# ----------------------------------------------------------------------------

VAT_TABLE: dict[str, tuple[float, str]] = {
    "AE": (0.050, "United Arab Emirates"),
    "AT": (0.200, "Austria"),
    "AU": (0.100, "Australia"),
    "BD": (0.150, "Bangladesh"),
    "BE": (0.210, "Belgium"),
    "BG": (0.200, "Bulgaria"),
    "BS": (0.100, "Bahamas"),
    "BY": (0.200, "Belarus"),
    "CH": (0.081, "Switzerland"),
    "CL": (0.190, "Chile"),
    "CN": (0.060, "China"),  # Steam tax FAQ uses XC code; Steam global cc=cn pricing
    "CO": (0.190, "Colombia"),
    "CY": (0.190, "Cyprus"),
    "CZ": (0.210, "Czech Republic"),
    "DE": (0.190, "Germany"),
    "DK": (0.250, "Denmark"),
    "EE": (0.240, "Estonia"),
    "EG": (0.140, "Egypt"),
    "ES": (0.210, "Spain"),
    "FI": (0.255, "Finland"),
    "FR": (0.200, "France"),
    "GB": (0.200, "United Kingdom"),
    "GR": (0.240, "Greece"),
    "HR": (0.250, "Croatia"),
    "HU": (0.270, "Hungary"),
    "ID": (0.110, "Indonesia"),
    "IE": (0.230, "Ireland"),
    "IM": (0.200, "Isle of Man"),
    "IN": (0.180, "India"),
    "IS": (0.240, "Iceland"),
    "IT": (0.220, "Italy"),
    "JP": (0.100, "Japan"),
    "KR": (0.100, "Korea, Republic of"),
    "KZ": (0.160, "Kazakhstan"),
    "LT": (0.210, "Lithuania"),
    "LU": (0.170, "Luxembourg"),
    "LV": (0.210, "Latvia"),
    "MA": (0.200, "Morocco"),
    "MC": (0.200, "Monaco"),
    "MD": (0.200, "Moldova"),
    "MT": (0.180, "Malta"),
    "MX": (0.160, "Mexico"),
    "MY": (0.080, "Malaysia"),
    "NL": (0.210, "Netherlands"),
    "NO": (0.250, "Norway"),
    "NZ": (0.150, "New Zealand"),
    "PE": (0.180, "Peru"),
    "PH": (0.120, "Philippines"),
    "PL": (0.230, "Poland"),
    "PT": (0.230, "Portugal"),
    "RO": (0.210, "Romania"),
    "RS": (0.200, "Serbia"),
    "RU": (0.220, "Russian Federation"),
    "SA": (0.150, "Saudi Arabia"),
    "SE": (0.250, "Sweden"),
    "SG": (0.090, "Singapore"),
    "SI": (0.220, "Slovenia"),
    "SK": (0.230, "Slovakia"),
    "TH": (0.070, "Thailand"),
    "TR": (0.200, "Turkey"),
    "TW": (0.050, "Taiwan"),
    "UA": (0.200, "Ukraine"),
    "UZ": (0.120, "Uzbekistan"),
    "ZA": (0.150, "South Africa"),
}

# Extra countries without inclusive VAT but with their own Steam currency
# or special USD tier. VAT = 0 for these.
EXTRA_COUNTRIES: dict[str, str] = {
    "US": "United States",
    "CA": "Canada",
    "BR": "Brazil",
    "AR": "Argentina",
    "IL": "Israel",
    "HK": "Hong Kong",
    "VN": "Vietnam",
    "CR": "Costa Rica",
    "UY": "Uruguay",
    "KW": "Kuwait",
    "QA": "Qatar",
}


def all_countries() -> dict[str, tuple[float, str]]:
    out: dict[str, tuple[float, str]] = {}
    for cc, (rate, name) in VAT_TABLE.items():
        out[cc] = (rate, name)
    for cc, name in EXTRA_COUNTRIES.items():
        if cc not in out:
            out[cc] = (0.0, name)
    return out


# ----------------------------------------------------------------------------
# USD tiers. Steam Store API returns currency="USD" for several different
# price tiers; we differentiate them by cc manually.
# ----------------------------------------------------------------------------

USD_TIER_BY_CC: dict[str, str] = {
    # CIS USD tier
    "BY": "USD_CIS",
    "MD": "USD_CIS",
    "RU": "USD_CIS",   # if Steam answered USD (after dropping RUB)
    "UA": "USD_CIS",
    "KZ": "USD_CIS",
    "UZ": "USD_CIS",

    # South Asia USD tier
    "BD": "USD_SASIA",

    # MENA USD tier
    "MA": "USD_MENA",
    "EG": "USD_MENA",
    "KW": "USD_MENA",
    "QA": "USD_MENA",
    "TR": "USD_MENA",  # if Steam moved Turkey to USD
    "SA": "USD_MENA",  # if Steam answered USD instead of SAR

    # LATAM USD tier
    "AR": "USD_LATAM",

    # Default USD (US, CA, etc.) — countries not in this map → "USD"
}

# ----------------------------------------------------------------------------
# Steam currency directory → package, optional VAT override, display name.
# package: ROW | ASIA | CN_ONLY | RU_CIS | LATAM | MENA
# vat_override: if set, used instead of the representative country's VAT.
# ----------------------------------------------------------------------------

CURRENCY_INFO: dict[str, dict] = {
    # ROW: EU, AU, CA, NZ, NO, PL, CH, GB, US
    "USD":       {"package": "ROW",     "name": "US Dollar",         "vat_override": None},
    "EUR":       {"package": "ROW",     "name": "Euro",              "vat_override": 0.21},
    "GBP":       {"package": "ROW",     "name": "British Pound",     "vat_override": None},
    "AUD":       {"package": "ROW",     "name": "Australian Dollar", "vat_override": None},
    "CAD":       {"package": "ROW",     "name": "Canadian Dollar",   "vat_override": None},
    "CHF":       {"package": "ROW",     "name": "Swiss Franc",       "vat_override": None},
    "NOK":       {"package": "ROW",     "name": "Norwegian Krone",   "vat_override": None},
    "NZD":       {"package": "ROW",     "name": "NZ Dollar",         "vat_override": None},
    "PLN":       {"package": "ROW",     "name": "Polish Złoty",      "vat_override": None},

    # ASIA: HK, IN, ID, MY, PH, SG, TW, TH, USD_SASIA, VN, JP, KR
    "JPY":       {"package": "ASIA",    "name": "Japanese Yen",      "vat_override": None},
    "KRW":       {"package": "ASIA",    "name": "Korean Won",        "vat_override": None},
    "TWD":       {"package": "ASIA",    "name": "Taiwan Dollar",     "vat_override": None},
    "HKD":       {"package": "ASIA",    "name": "Hong Kong Dollar",  "vat_override": None},
    "SGD":       {"package": "ASIA",    "name": "Singapore Dollar",  "vat_override": None},
    "MYR":       {"package": "ASIA",    "name": "Malaysian Ringgit", "vat_override": None},
    "THB":       {"package": "ASIA",    "name": "Thai Baht",         "vat_override": None},
    "IDR":       {"package": "ASIA",    "name": "Indonesian Rupiah", "vat_override": None},
    "PHP":       {"package": "ASIA",    "name": "Philippine Peso",   "vat_override": None},
    "VND":       {"package": "ASIA",    "name": "Vietnamese Dong",   "vat_override": None},
    "INR":       {"package": "ASIA",    "name": "Indian Rupee",      "vat_override": None},
    "USD_SASIA": {"package": "ASIA",    "name": "USD (S. Asia tier)", "vat_override": 0.0},

    # CN_ONLY: CNY only
    "CNY":       {"package": "CN_ONLY", "name": "Chinese Yuan",      "vat_override": None},

    # RU_CIS: RUB, USD_CIS, KZT, UAH
    "RUB":       {"package": "RU_CIS",  "name": "Russian Ruble",     "vat_override": None},
    "UAH":       {"package": "RU_CIS",  "name": "Ukrainian Hryvnia", "vat_override": None},
    "KZT":       {"package": "RU_CIS",  "name": "Kazakhstani Tenge", "vat_override": None},
    "USD_CIS":   {"package": "RU_CIS",  "name": "USD (CIS tier)",    "vat_override": 0.0},

    # LATAM: BR, CL, CO, CR, USD_LATAM, MX, PE, UY
    "BRL":       {"package": "LATAM",   "name": "Brazilian Real",    "vat_override": None},
    "MXN":       {"package": "LATAM",   "name": "Mexican Peso",      "vat_override": None},
    "CLP":       {"package": "LATAM",   "name": "Chilean Peso",      "vat_override": None},
    "COP":       {"package": "LATAM",   "name": "Colombian Peso",    "vat_override": None},
    "PEN":       {"package": "LATAM",   "name": "Peruvian Sol",      "vat_override": None},
    "UYU":       {"package": "LATAM",   "name": "Uruguayan Peso",    "vat_override": None},
    "CRC":       {"package": "LATAM",   "name": "Costa Rican Colón", "vat_override": None},
    "USD_LATAM": {"package": "LATAM",   "name": "USD (LATAM tier)",  "vat_override": 0.0},

    # MENA: IL, KW, QA, SA, ZA, USD_MENA, AE
    "ILS":       {"package": "MENA",    "name": "Israeli Shekel",    "vat_override": None},
    "AED":       {"package": "MENA",    "name": "UAE Dirham",        "vat_override": None},
    "SAR":       {"package": "MENA",    "name": "Saudi Riyal",       "vat_override": None},
    "QAR":       {"package": "MENA",    "name": "Qatari Riyal",      "vat_override": None},
    "KWD":       {"package": "MENA",    "name": "Kuwaiti Dinar",     "vat_override": None},
    "ZAR":       {"package": "MENA",    "name": "South African Rand", "vat_override": None},
    "USD_MENA":  {"package": "MENA",    "name": "USD (MENA tier)",   "vat_override": 0.0},
}

# Base currency per package
PACKAGE_BASE_CURRENCY: dict[str, str] = {
    "ROW":     "EUR",
    "ASIA":    "USD_SASIA",
    "CN_ONLY": "CNY",
    "RU_CIS":  "RUB",
    "LATAM":   "BRL",
    "MENA":    "ILS",
}

PACKAGE_DISPLAY: dict[str, str] = {
    "ROW":     "🌍 ROW (Rest of World)",
    "ASIA":    "🌏 Asia",
    "CN_ONLY": "🇨🇳 CN Only",
    "RU_CIS":  "🇷🇺 RU-CIS",
    "LATAM":   "🌎 LATAM",
    "MENA":    "🕌 MENA",
}

PACKAGE_ORDER = ["ROW", "ASIA", "CN_ONLY", "RU_CIS", "LATAM", "MENA"]

STEAM_API = "https://store.steampowered.com/api/appdetails"
FX_API = "https://open.er-api.com/v6/latest/USD"


# ----------------------------------------------------------------------------
# Network calls (cached)
# ----------------------------------------------------------------------------

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_app_meta(appid: str) -> dict | None:
    try:
        r = requests.get(
            STEAM_API,
            params={"appids": appid, "filters": "basic", "l": "en"},
            timeout=15,
        )
        r.raise_for_status()
        node = (r.json() or {}).get(appid) or {}
        if node.get("success"):
            return node.get("data") or {}
    except Exception:
        pass
    return None


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_steam_price(appid: str, cc: str) -> dict | None:
    try:
        r = requests.get(
            STEAM_API,
            params={
                "appids": appid, "cc": cc,
                "filters": "price_overview", "l": "en",
            },
            timeout=15,
        )
        r.raise_for_status()
        node = (r.json() or {}).get(appid) or {}
        if not node.get("success"):
            return None
        po = (node.get("data") or {}).get("price_overview")
        if not po:
            return None
        return {
            "currency": po.get("currency"),
            "final": po.get("final"),
            "initial": po.get("initial"),
            "discount_percent": po.get("discount_percent", 0),
            "final_formatted": po.get("final_formatted", ""),
        }
    except Exception:
        return None


@st.cache_data(ttl=900, show_spinner=False)
def fetch_fx_rates() -> tuple[dict[str, float], str]:
    try:
        r = requests.get(FX_API, timeout=15)
        r.raise_for_status()
        data = r.json() or {}
        return data.get("rates") or {}, data.get("time_last_update_utc") or ""
    except Exception:
        return {}, ""


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def steam_minor_to_major(amount_minor) -> float | None:
    """Steam Store API always uses /100, including JPY/KRW."""
    if amount_minor is None:
        return None
    try:
        return float(amount_minor) / 100.0
    except (TypeError, ValueError):
        return None


def relabel_currency(cc: str, currency: str) -> str:
    """USD → USD_CIS / USD_SASIA / USD_MENA / USD_LATAM by cc."""
    if currency != "USD":
        return currency
    return USD_TIER_BY_CC.get(cc, "USD")


def floor_to_99(x: float) -> float:
    """
    Psychological rounding down to the nearest N.99.
    Examples:
      50.24 → 49.99
      50.99 → 50.99 (already on the boundary)
      51.00 → 50.99
      9.50  → 8.99
      0.50  → 0.50  (values <1 are left alone)
    """
    if x is None:
        return None
    if x < 1:
        return round(x, 2)
    n = int(x)  # floor for positive numbers
    boundary = n + 0.99
    if x >= boundary - 1e-9:
        return round(boundary, 2)
    if n >= 1:
        return round(n - 1 + 0.99, 2)
    return round(x, 2)


# ----------------------------------------------------------------------------
# Valve suggested-pricing snapshot (Mode B)
# ----------------------------------------------------------------------------
#
# Steam's official suggested pricing matrix lives in the partner backend
# (https://partner.steamgames.com/pricing/explorer — page is public but data
# is loaded from a confidential endpoint). The snapshot below is captured by
# manual readout of two anchor tiers in the explorer using the
# **Multi-variable conversion** method (PPP + FX + comparable-entertainment
# cost). Snapshot date: data the explorer reported as "January 2026".
#
# We store actual Valve prices for the two anchors and linearly interpolate
# for the other 39 tiers. After interpolation we ψ-round per currency so the
# numbers look Valve-like (.99 endings or N99 for zero-decimal currencies).

# Official Valve USD price tiers (41 tiers, from the explorer's tier picker)
VALVE_TIERS: list[float] = [
    0.99, 1.99, 2.99, 3.99, 4.99, 5.99, 6.99, 7.99, 8.99, 9.99,
    10.99, 11.99, 12.99, 13.99, 14.99, 15.99, 16.99, 17.99, 18.99, 19.99,
    24.99, 29.99, 34.99, 39.99, 44.99, 49.99, 54.99, 59.99,
    64.99, 69.99, 74.99, 79.99, 84.99, 89.99, 99.99,
    109.99, 119.99, 129.99, 139.99, 149.99, 199.99,
]

# Two anchor points. Each maps tier_currency → local price in the currency.
# Both anchors taken from Valve's pricing/explorer with Multi-variable method.
ANCHOR_LOW = 9.99
ANCHOR_HIGH = 59.99

VALVE_PRICE_TABLE: dict[float, dict[str, float]] = {
    9.99: {
        "USD": 9.99,
        "GBP": 9.09,    "EUR": 10.25,   "CHF": 8.75,
        "AUD": 13.95,   "CAD": 11.99,   "NZD": 15.75,
        "NOK": 120.00,  "PLN": 42.49,
        # ASIA
        "JPY": 1350,    "KRW": 10500,   "TWD": 216,
        "HKD": 61.00,   "SGD": 11.25,   "MYR": 25.49,
        "THB": 205.00,  "IDR": 94499,   "PHP": 329.00,
        "VND": 149500,  "INR": 499,     "USD_SASIA": 6.29,
        # CN
        "CNY": 42.00,
        # RU-CIS
        "RUB": 465,     "UAH": 230,     "KZT": 3190,    "USD_CIS": 6.29,
        # LATAM
        "BRL": 37.49,   "MXN": 139.99,  "CLP": 6599,    "COP": 26999,
        "PEN": 25.99,   "UYU": 348,     "CRC": 5200,    "USD_LATAM": 6.29,
        # MENA
        "ILS": 35.99,   "AED": 32.75,   "SAR": 25.75,   "QAR": 28.49,
        "KWD": 2.20,    "ZAR": 104.99,  "USD_MENA": 6.29,
    },
    59.99: {
        "USD": 59.99,
        "GBP": 53.49,   "EUR": 61.99,   "CHF": 52.49,
        "AUD": 83.95,   "CAD": 71.99,   "NZD": 91.99,
        "NOK": 720.00,  "PLN": 254.99,
        # ASIA
        "JPY": 7350,    "KRW": 61500,   "TWD": 1030,
        "HKD": 336.00,  "SGD": 54.99,   "MYR": 129.99,
        "THB": 1049,    "IDR": 469999,  "PHP": 1649.00,
        "VND": 743000,  "INR": 2499,    "USD_SASIA": 28.25,
        # CN
        "CNY": 200.00,
        # RU-CIS
        "RUB": 2300,    "UAH": 1150,    "KZT": 15400,   "USD_CIS": 28.25,
        # LATAM
        "BRL": 184.99,  "MXN": 699.99,  "CLP": 32999,   "COP": 134999,
        "PEN": 129.99,  "UYU": 1910,    "CRC": 27000,   "USD_LATAM": 28.25,
        # MENA
        "ILS": 219.99,  "AED": 174.99,  "SAR": 129.99,  "QAR": 136.99,
        "KWD": 10.95,   "ZAR": 519.99,  "USD_MENA": 28.25,
    },
}

# Representative cc per tier (used to synthesize raw_results in Mode B).
TIER_REPRESENTATIVE_CC: dict[str, str] = {
    # ROW
    "USD": "US", "EUR": "DE", "GBP": "GB", "AUD": "AU", "CAD": "CA",
    "CHF": "CH", "NOK": "NO", "NZD": "NZ", "PLN": "PL",
    # ASIA
    "JPY": "JP", "KRW": "KR", "TWD": "TW", "HKD": "HK", "SGD": "SG",
    "MYR": "MY", "THB": "TH", "IDR": "ID", "PHP": "PH", "VND": "VN",
    "INR": "IN", "USD_SASIA": "BD",
    # CN
    "CNY": "CN",
    # RU-CIS
    "RUB": "RU", "UAH": "UA", "KZT": "KZ", "USD_CIS": "BY",
    # LATAM
    "BRL": "BR", "MXN": "MX", "CLP": "CL", "COP": "CO",
    "PEN": "PE", "UYU": "UY", "CRC": "CR", "USD_LATAM": "AR",
    # MENA
    "ILS": "IL", "AED": "AE", "SAR": "SA", "QAR": "QA", "KWD": "KW",
    "ZAR": "ZA", "USD_MENA": "MA",
}

# Currencies that don't use decimal subunits in normal Steam pricing
# (ψ-rounding snaps to N99 integer instead of .99 decimal). Membership based
# on observed Valve pricing/explorer values: these currencies always come back
# as integers (₽465, ¥1350, ₸3190, etc.) — never with a decimal subunit.
ZERO_DECIMAL_CURRENCIES: set[str] = {
    "JPY", "KRW", "IDR", "VND", "CLP", "COP", "KZT", "UYU", "CRC",
    "RUB", "UAH", "INR", "TWD", "PHP", "THB",
}


def round_psy_currency(price: float, currency: str) -> float:
    """
    Per-currency psychological rounding for interpolated Mode B prices.
    Decimal currencies → floor_to_99 (e.g., 14.32 → 13.99).
    Zero-decimal      → snap to nearest integer ending in 99 (e.g., 1948 → 1899).
    """
    if price is None:
        return None
    if currency in ZERO_DECIMAL_CURRENCIES:
        n = int(round(price))
        if n < 100:
            return float(n)
        rounded = round(n / 100.0) * 100
        return float(int(rounded) - 1)
    return floor_to_99(price)


def interpolate_valve_price(usd_tier: float, currency: str) -> float | None:
    """
    Two-anchor linear interpolation between Valve's $9.99 and $59.99 columns.
    For the anchor tiers themselves, returns the exact Valve number.
    For other tiers, linearly interpolates and applies ψ-rounding so the
    output looks Valve-like (.99 / N99 endings).

    Tiers above $59.99 or below $9.99 use linear extrapolation (less accurate
    for extreme tiers — refresh anchors in VALVE_PRICE_TABLE if needed).
    """
    p_low = VALVE_PRICE_TABLE[ANCHOR_LOW].get(currency)
    p_high = VALVE_PRICE_TABLE[ANCHOR_HIGH].get(currency)
    if p_low is None or p_high is None:
        return None

    # Exact match for anchors — return the Valve number unmodified.
    if abs(usd_tier - ANCHOR_LOW) < 1e-6:
        return float(p_low)
    if abs(usd_tier - ANCHOR_HIGH) < 1e-6:
        return float(p_high)

    # Linear interpolation in USD-tier space.
    t = (usd_tier - ANCHOR_LOW) / (ANCHOR_HIGH - ANCHOR_LOW)
    raw = p_low + (p_high - p_low) * t

    return round_psy_currency(raw, currency)


def synthesize_raw_results_from_usd(base_usd: float) -> dict[str, dict]:
    """
    Build a synthetic raw_results dict (cc → price_overview) from a USD anchor
    using Valve's two-anchor matrix + linear interpolation + ψ-rounding.

    Output mirrors build_pricing_table()'s raw_results so it plugs into
    build_recommendations() unchanged.
    """
    raw: dict[str, dict] = {}
    for tier in VALVE_PRICE_TABLE[ANCHOR_LOW].keys():
        rep_cc = TIER_REPRESENTATIVE_CC.get(tier)
        if not rep_cc:
            continue

        local_price = interpolate_valve_price(base_usd, tier)
        if local_price is None:
            continue

        # USD-tier currency string is "USD" — relabel_currency in the recommender
        # converts (cc, "USD") → USD_CIS / USD_SASIA / etc. via USD_TIER_BY_CC.
        currency_str = "USD" if tier.startswith("USD") else tier

        # Steam Store API stores prices /100 (always, including JPY/KRW).
        final_minor = int(round(local_price * 100))

        raw[rep_cc] = {
            "currency": currency_str,
            "final": final_minor,
            "initial": final_minor,
            "discount_percent": 0,
            "final_formatted": f"{currency_str} {local_price:.2f}",
        }
    return raw


# ----------------------------------------------------------------------------
# Build per-country detailed table (current Mode A logic)
# ----------------------------------------------------------------------------

def compute_country_row(
    cc: str,
    country_name: str,
    vat_rate: float,
    price_info: dict | None,
    fx_rates: dict[str, float],
    distributor_fee_pct: float,
) -> dict:
    base = {
        "Region": f"{cc} — {country_name}",
        "cc": cc,
        "Currency": "—",
        "Local price": None,
        "USD price (FX)": None,
        "VAT %": f"{vat_rate * 100:.1f}%" if vat_rate > 0 else "0%",
        "USD price ex-VAT": None,
        "Publisher revenue (local)": None,
        "Publisher revenue (USD)": None,
        "Note": "",
    }
    if not price_info:
        base["Note"] = "no price (free / not for sale)"
        return base

    currency = price_info.get("currency") or "—"
    local_price = steam_minor_to_major(price_info.get("final"))
    if local_price is None or local_price <= 0:
        base["Note"] = "no price"
        base["Currency"] = currency
        return base

    local_ex_vat = local_price / (1 + vat_rate) if vat_rate > 0 else local_price
    publisher_local = local_ex_vat * (1 - distributor_fee_pct / 100.0)

    rate = fx_rates.get(currency)
    if rate and rate > 0:
        usd_gross = local_price / rate
        usd_ex_vat = local_ex_vat / rate
        publisher_usd = publisher_local / rate
    else:
        usd_gross = usd_ex_vat = publisher_usd = None
        base["Note"] = f"no FX rate for {currency}"

    base.update({
        "Currency": currency,
        "Local price": round(local_price, 2),
        "USD price (FX)": round(usd_gross, 2) if usd_gross is not None else None,
        "USD price ex-VAT": round(usd_ex_vat, 2) if usd_ex_vat is not None else None,
        "Publisher revenue (local)": round(publisher_local, 2),
        "Publisher revenue (USD)": round(publisher_usd, 2) if publisher_usd is not None else None,
    })
    return base


def build_pricing_table(appid: str, distributor_fee_pct: float, progress_cb=None):
    countries = all_countries()
    fx_rates, fx_last = fetch_fx_rates()

    results: dict[str, dict | None] = {}
    total = len(countries)
    done = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        future_to_cc = {ex.submit(fetch_steam_price, appid, cc): cc for cc in countries}
        for fut in as_completed(future_to_cc):
            cc = future_to_cc[fut]
            try:
                results[cc] = fut.result()
            except Exception:
                results[cc] = None
            done += 1
            if progress_cb:
                progress_cb(done / total)

    rows = []
    for cc, (vat_rate, name) in countries.items():
        rows.append(compute_country_row(
            cc=cc, country_name=name, vat_rate=vat_rate,
            price_info=results.get(cc), fx_rates=fx_rates,
            distributor_fee_pct=distributor_fee_pct,
        ))
    df = pd.DataFrame(rows)
    return df, fx_rates, fx_last, results


# ----------------------------------------------------------------------------
# Pricing recommendations: dedup, grouping, raise-only, ψ-rounding
# ----------------------------------------------------------------------------

def deduplicate_by_currency_tier(
    per_country_results: dict[str, dict | None],
    countries: dict[str, tuple[float, str]],
) -> dict[str, dict]:
    """
    Returns {tier: {cc, country_name, currency, local_price, vat_country}}.
    Picks the first country we observed for each tier (for USD tiers, the
    first cc matching USD_TIER_BY_CC).
    """
    tiers: dict[str, dict] = {}
    # Iterate in deterministic order (USD tiers first, then the rest).
    # Sort by cc so EUR ends up represented by DE rather than AT/BE.
    for cc in sorted(per_country_results.keys()):
        data = per_country_results.get(cc)
        if not data:
            continue
        currency = data.get("currency")
        if not currency:
            continue
        tier = relabel_currency(cc, currency)
        if tier in tiers:
            continue  # already have a representative for this tier
        local_price = steam_minor_to_major(data.get("final"))
        if local_price is None or local_price <= 0:
            continue
        vat_country, country_name = countries.get(cc, (0.0, cc))
        tiers[tier] = {
            "tier": tier,
            "cc": cc,
            "country_name": country_name,
            "currency_raw": currency,
            "local_price": local_price,
            "vat_country": vat_country,
            "discount_pct": data.get("discount_percent", 0),
        }
    return tiers


def vat_for_tier(tier: str, vat_country: float) -> float:
    info = CURRENCY_INFO.get(tier)
    if not info:
        return vat_country
    override = info.get("vat_override")
    return override if override is not None else vat_country


def fx_rate_for_tier(tier: str, fx_rates: dict[str, float]) -> float | None:
    """USD tiers convert at USD (rate=1). Others convert by their own currency."""
    if tier.startswith("USD"):
        return 1.0
    return fx_rates.get(tier)


def compute_publisher_usd(
    local_price: float,
    vat: float,
    distributor_fee_pct: float,
    fx_rate: float | None,
) -> float | None:
    if fx_rate is None or fx_rate <= 0:
        return None
    local_ex_vat = local_price / (1 + vat) if vat > 0 else local_price
    pub_local = local_ex_vat * (1 - distributor_fee_pct / 100.0)
    return pub_local / fx_rate


def reverse_to_retail_usd(
    target_pub_usd: float,
    vat: float,
    distributor_fee_pct: float,
) -> float:
    """
    Reverse formula. FX cancels out, so only VAT and dist_fee remain.
        retail_usd = target_pub_usd * (1 + vat) / (1 - dist_fee)
    """
    return target_pub_usd * (1 + vat) / (1 - distributor_fee_pct / 100.0)


def build_recommendations(
    per_country_results: dict[str, dict | None],
    fx_rates: dict[str, float],
    distributor_fee_pct: float,
) -> dict[str, dict]:
    """
    Returns {package: {"base_tier", "base_pub_usd", "rows": [...] }}.
    """
    countries = all_countries()
    deduped = deduplicate_by_currency_tier(per_country_results, countries)

    # Compute publisher_usd for each tier
    enriched: dict[str, dict] = {}
    for tier, data in deduped.items():
        info = CURRENCY_INFO.get(tier)
        if not info:
            continue
        vat = vat_for_tier(tier, data["vat_country"])
        fx = fx_rate_for_tier(tier, fx_rates)
        pub_usd = compute_publisher_usd(
            data["local_price"], vat, distributor_fee_pct, fx
        )
        retail_usd = (data["local_price"] / fx) if (fx and fx > 0) else None
        enriched[tier] = {
            **data,
            "package": info["package"],
            "vat": vat,
            "fx": fx,
            "current_pub_usd": pub_usd,
            "current_retail_usd": retail_usd,
        }

    # Group by package
    by_package: dict[str, list[dict]] = {pkg: [] for pkg in PACKAGE_ORDER}
    for tier, item in enriched.items():
        pkg = item["package"]
        if pkg in by_package:
            by_package[pkg].append(item)

    # Per package: find the base, compute target and recommendations
    results: dict[str, dict] = {}
    for pkg, items in by_package.items():
        if not items:
            results[pkg] = {"base_tier": PACKAGE_BASE_CURRENCY[pkg], "base_pub_usd": None, "rows": []}
            continue

        base_tier = PACKAGE_BASE_CURRENCY[pkg]
        base = next((i for i in items if i["tier"] == base_tier), None)
        target_pub_usd = base["current_pub_usd"] if base else None

        rows = []
        for item in items:
            current_pub = item["current_pub_usd"]
            is_base = item["tier"] == base_tier

            if target_pub_usd is None or current_pub is None:
                rec_pub = current_pub
                delta = None
                gap_pct = None
            else:
                # Raise-only: max(current, base)
                rec_pub = max(current_pub, target_pub_usd)
                delta = rec_pub - current_pub
                # gap_pct = how far current is below target, as a fraction of target
                # > 0 → we need to raise;  ≤ 0 → already at or above base
                if target_pub_usd > 0:
                    gap_pct = (target_pub_usd - current_pub) / target_pub_usd
                else:
                    gap_pct = 0.0

            # If we are not changing the price (delta == 0 or None), keep retail
            # as-is. ψ-rounding is applied only when we are actually raising.
            EPS = 1e-6
            should_change_price = (
                delta is not None and delta > EPS
            )

            if should_change_price:
                rec_retail_usd_raw = reverse_to_retail_usd(
                    rec_pub, item["vat"], distributor_fee_pct
                )
                rec_retail_usd_psy = floor_to_99(rec_retail_usd_raw)
                # local computed from ψ-rounded retail USD
                if item["fx"]:
                    rec_retail_local = rec_retail_usd_psy * item["fx"]
                else:
                    rec_retail_local = None
            else:
                # No change — recommendation equals current retail.
                rec_retail_usd_raw = item["current_retail_usd"]
                rec_retail_usd_psy = item["current_retail_usd"]
                rec_retail_local = item["local_price"]

            rows.append({
                "is_base": is_base,
                "is_changed": should_change_price,
                "gap_pct": gap_pct,
                "tier": item["tier"],
                "tier_label": ("⭐ " if is_base else "") + item["tier"],
                "country": f"{item['cc']} — {item['country_name']}",
                "currency_raw": item["currency_raw"],
                "vat": item["vat"],
                "vat_pct": f"{item['vat']*100:.1f}%",
                "current_local_price": round(item["local_price"], 2),
                "current_retail_usd": round(item["current_retail_usd"], 2)
                    if item["current_retail_usd"] is not None else None,
                "current_pub_usd": round(current_pub, 2) if current_pub is not None else None,
                "target_pub_usd": round(target_pub_usd, 2) if target_pub_usd is not None else None,
                "rec_pub_usd": round(rec_pub, 2) if rec_pub is not None else None,
                "delta_pub_usd": round(delta, 2) if delta is not None else None,
                "gap_pct_str": f"{gap_pct*100:+.1f}%" if gap_pct is not None else "—",
                "rec_retail_usd_raw": round(rec_retail_usd_raw, 2) if rec_retail_usd_raw is not None else None,
                "rec_retail_usd_psy": round(rec_retail_usd_psy, 2) if rec_retail_usd_psy is not None else None,
                "rec_retail_local": round(rec_retail_local, 2) if rec_retail_local is not None else None,
            })

        # Sort: base on top, then by descending delta_pub_usd
        rows.sort(key=lambda r: (
            0 if r["is_base"] else 1,
            -(r["delta_pub_usd"] or 0)
        ))
        results[pkg] = {
            "base_tier": base_tier,
            "base_pub_usd": round(target_pub_usd, 2) if target_pub_usd is not None else None,
            "rows": rows,
        }

    return results


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------

def _row_style(row: pd.Series) -> list[str]:
    """
    Recommendation row styling:
      • orange — price was raised (delta > 0)
      • pink — raised AND gap > 15% (large gap from base)
      • no color — price unchanged (base, or already above base)
    """
    is_changed = bool(row.get("_is_changed"))
    gap = row.get("_gap_pct") or 0.0

    if not is_changed:
        return [""] * len(row)
    if gap > 0.15:
        color = f"background-color: {ROW_TINT_PINK}"
    else:
        color = f"background-color: {ROW_TINT_ORANGE}"
    return [color] * len(row)


def _package_header(pkg: str, block: dict) -> str:
    """Build the expander title for a package."""
    title = PACKAGE_DISPLAY.get(pkg, pkg)
    base_tier = block.get("base_tier")
    base_pub_usd = block.get("base_pub_usd")
    if base_pub_usd is None:
        return f"{title} · base: {base_tier} (no price)"
    return f"{title} · base: {base_tier} · target ${base_pub_usd:.2f}"


def _render_removal_candidates(rows: list[dict]) -> None:
    """Alternative to raising the price: list currencies that could be dropped from distribution."""
    candidates = [
        r for r in rows
        if r["is_changed"]
        and r["gap_pct"] is not None
        and r["gap_pct"] > 0.05
    ]
    if candidates:
        st.markdown("**Removal candidates** (if raising the price is not an option):")
        items_html = []
        for r in candidates:
            color = BRAND["pink"] if r["gap_pct"] > 0.15 else BRAND["orange"]
            items_html.append(
                f'<li><span class="legend-dot" style="background:{color};"></span>'
                f'<b>{r["tier"]}</b> — raise to ${r["rec_pub_usd"]} '
                f'(currently ${r["current_pub_usd"]}), or remove from distribution</li>'
            )
        st.markdown(
            f'<ul style="margin: 4px 0 0 0; padding-left: 18px; line-height: 1.7;">{"".join(items_html)}</ul>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f'<div style="color:{BRAND["green"]}; font-weight:500; margin-top:8px;">'
            f'✓ All currencies in this package are within 5% of base — no removal candidates.'
            f'</div>',
            unsafe_allow_html=True,
        )


def _render_simplified_table(rows: list[dict], current_col_label: str) -> None:
    """
    3-column table for the Recommendations tab:
      Tier | <Current Local Price | Current Steam Price> | Recommended Local Price

    Row coloring (orange/pink for raised currencies) is applied via a
    precomputed style array so the service columns never leak into the UI.
    """
    df = pd.DataFrame(rows)

    display = df.rename(columns={
        "tier_label": "Tier",
        "current_local_price": current_col_label,
        "rec_retail_local": "Recommended Local Price",
    })

    cols = ["Tier", current_col_label, "Recommended Local Price"]
    df_display = display[cols].copy()

    # Pre-compute row backgrounds without exposing service columns.
    def color_row(row: pd.Series) -> list[str]:
        is_changed = bool(df.loc[row.name, "is_changed"])
        gap = df.loc[row.name, "gap_pct"] or 0.0
        if not is_changed:
            return [""] * len(row)
        if gap > 0.15:
            return [f"background-color: {ROW_TINT_PINK}"] * len(row)
        return [f"background-color: {ROW_TINT_ORANGE}"] * len(row)

    styled = (
        df_display
        .style
        .apply(color_row, axis=1)
        .format({
            current_col_label:          "{:.2f}",
            "Recommended Local Price":  "{:.2f}",
        }, na_rep="—")
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)


def _render_full_table(rows: list[dict]) -> None:
    """Full detailed table for the Details tab — all metrics."""
    df = pd.DataFrame(rows)
    df["_is_changed"] = df["is_changed"]
    df["_gap_pct"] = df["gap_pct"]

    display = df.rename(columns={
        "tier_label": "Tier",
        "country": "Representative",
        "currency_raw": "Steam currency",
        "vat_pct": "VAT %",
        "current_local_price": "Current local",
        "current_retail_usd": "Current USD retail",
        "current_pub_usd": "Current pub USD",
        "target_pub_usd": "Target pub USD",
        "rec_pub_usd": "Rec pub USD",
        "delta_pub_usd": "Δ pub USD",
        "gap_pct_str": "Gap",
        "rec_retail_usd_raw": "Rec retail USD (raw)",
        "rec_retail_usd_psy": "Rec retail USD (.99)",
        "rec_retail_local": "Rec retail local",
    })

    cols = [
        "Tier", "Steam currency", "Representative", "VAT %",
        "Current local", "Current USD retail",
        "Current pub USD", "Target pub USD", "Gap",
        "Rec pub USD", "Δ pub USD",
        "Rec retail USD (raw)", "Rec retail USD (.99)",
        "Rec retail local",
    ]

    styled = (
        display[cols + ["_is_changed", "_gap_pct"]]
        .style
        .apply(_row_style, axis=1)
        .hide(subset=["_is_changed", "_gap_pct"], axis="columns")
        .format({
            "Current local":          "{:.2f}",
            "Current USD retail":     "${:.2f}",
            "Current pub USD":        "${:.2f}",
            "Target pub USD":         "${:.2f}",
            "Rec pub USD":            "${:.2f}",
            "Δ pub USD":              "{:+.2f}",
            "Rec retail USD (raw)":   "${:.2f}",
            "Rec retail USD (.99)":   "${:.2f}",
            "Rec retail local":       "{:.2f}",
        }, na_rep="—")
    )
    st.dataframe(styled, use_container_width=True, hide_index=True)


def render_recommendations(rec: dict[str, dict], mode: str) -> None:
    """
    Render results as two tabs:
      • Recommendations — simplified 4-column table per package
      • Details        — full per-currency metrics
    `mode` must be "appid" (Mode A) or "base_usd" (Mode B). It only affects
    the label of the "current" column in the simplified table.
    """
    current_col = (
        "Current Steam Price" if mode == "base_usd" else "Current Local Price"
    )

    tab_rec, tab_details = st.tabs(["🎯 Recommendations", "📋 Details"])

    with tab_rec:
        st.markdown(
            "**We recommend you to set the following prices for each region, "
            "and also create regional SKUs.**"
        )
        for pkg in PACKAGE_ORDER:
            block = rec.get(pkg, {})
            rows = block.get("rows", [])
            if not rows:
                with st.expander(f"{PACKAGE_DISPLAY.get(pkg, pkg)} — no data", expanded=False):
                    st.info("No prices for this package.")
                continue
            with st.expander(_package_header(pkg, block), expanded=(pkg == "ROW")):
                _render_simplified_table(rows, current_col)
                _render_removal_candidates(rows)

    with tab_details:
        for pkg in PACKAGE_ORDER:
            block = rec.get(pkg, {})
            rows = block.get("rows", [])
            if not rows:
                continue
            with st.expander(_package_header(pkg, block), expanded=(pkg == "ROW")):
                _render_full_table(rows)


def main() -> None:
    st.set_page_config(
        page_title="Price Recommendation Tool by Rokky",
        page_icon="💰",
        layout="wide",
    )

    inject_css()
    render_logo()

    st.title("Price Recommendation Tool by Rokky")
    st.caption(
        "Calculates publisher revenue from Steam regional prices net of VAT "
        "and the distributor fee, and recommends target prices per package "
        "(ROW / Asia / CN / RU-CIS / LATAM / MENA) to guard against "
        "cross-border arbitrage."
    )

    with st.sidebar:
        st.header("Parameters")

        mode = st.radio(
            "Input mode",
            options=[
                "Steam AppID (live prices)",
                "Base USD price (Valve matrix)",
            ],
            index=0,
            help=(
                "AppID — fetch live regional prices from the Steam Store API.\n\n"
                "Base USD — use Valve's suggested-pricing matrix from "
                "partner pricing/explorer (Multi-variable conversion). Anchored "
                "at $9.99 and $59.99 with linear interpolation for the other "
                "39 tiers."
            ),
        )

        appid = ""
        base_usd = 0.0
        if mode.startswith("Steam AppID"):
            appid = st.text_input(
                "Steam AppID",
                value="730",
                help="e.g. 730 = Counter-Strike 2",
            ).strip()
        else:
            default_idx = VALVE_TIERS.index(29.99) if 29.99 in VALVE_TIERS else 0
            base_usd = st.selectbox(
                "Base USD price (Valve tier)",
                options=VALVE_TIERS,
                index=default_idx,
                format_func=lambda x: f"${x:.2f}",
                help=(
                    "Pick from Valve's 41 official USD tiers (from the partner "
                    "pricing/explorer). Exact Valve numbers are used for $9.99 "
                    "and $59.99 anchors; other tiers are linearly interpolated."
                ),
            )

        distributor_fee = st.number_input(
            "Distributor fee, %",
            min_value=0.0, max_value=99.0, value=20.0, step=0.5,
        )
        st.markdown("---")
        run = st.button("Calculate", type="primary", use_container_width=True)

    if not run:
        st.info("👈 Pick an input mode, set parameters, and click **Calculate**.")
        st.stop()

    # ---- Mode-specific input handling ----
    mode_label = "appid" if mode.startswith("Steam AppID") else "base_usd"

    if mode_label == "appid":
        if not appid.isdigit():
            st.error("AppID must be a number, e.g. `730`.")
            st.stop()

        meta = fetch_app_meta(appid)
        app_name = (meta or {}).get("name") or f"AppID {appid}"

        progress_bar = st.progress(0.0, text="Fetching prices from Steam Store API…")
        _df, fx_rates, fx_last, raw_results = build_pricing_table(
            appid=appid,
            distributor_fee_pct=distributor_fee,
            progress_cb=lambda p: progress_bar.progress(p, text=f"Fetching prices… {int(p*100)}%"),
        )
        progress_bar.empty()

        download_label_suffix = appid
        st.subheader(app_name)
    else:
        if base_usd <= 0:
            st.error("Base USD price must be greater than 0.")
            st.stop()

        with st.spinner("Loading Valve's suggested-pricing matrix…"):
            fx_rates, fx_last = fetch_fx_rates()
            raw_results = synthesize_raw_results_from_usd(base_usd)

        download_label_suffix = f"base_{base_usd:.2f}"
        st.subheader(f"Valve suggested pricing @ ${base_usd:.2f}")

    rec = build_recommendations(raw_results, fx_rates, distributor_fee)

    # ---- Two tabs: Recommendations + Details ----
    render_recommendations(rec, mode_label)

    # Combined CSV export of all recommendations
    all_rows = []
    for pkg in PACKAGE_ORDER:
        for r in rec.get(pkg, {}).get("rows", []):
            all_rows.append({
                "package": pkg,
                "is_base": r["is_base"],
                "is_changed": r["is_changed"],
                "tier": r["tier"],
                "country": r["country"],
                "vat_pct": r["vat_pct"],
                "current_local_price": r["current_local_price"],
                "current_retail_usd": r["current_retail_usd"],
                "current_pub_usd": r["current_pub_usd"],
                "target_pub_usd": r["target_pub_usd"],
                "rec_pub_usd": r["rec_pub_usd"],
                "delta_pub_usd": r["delta_pub_usd"],
                "gap_pct": round(r["gap_pct"], 4) if r["gap_pct"] is not None else None,
                "rec_retail_usd_raw": r["rec_retail_usd_raw"],
                "rec_retail_usd_psy": r["rec_retail_usd_psy"],
                "rec_retail_local": r["rec_retail_local"],
            })
    rec_df = pd.DataFrame(all_rows)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    st.download_button(
        "💾 Download CSV (recommendations)",
        data=rec_df.to_csv(index=False).encode("utf-8"),
        file_name=f"steam_pricing_rec_{download_label_suffix}_{ts}.csv",
        mime="text/csv",
    )

    st.markdown("---")
    st.caption(
        "**Sources:** "
        "[Steam Store API](https://store.steampowered.com/api/appdetails) · "
        "[Steam tax FAQ](https://partner.steamgames.com/doc/finance/taxfaq) · "
        "[open.er-api.com](https://open.er-api.com) (FX). "
        "Steam's 30% revenue share is NOT applied — calculation targets distributor (CD-key) sales. "
        "USD tiers (USD / USD_CIS / USD_SASIA / USD_MENA / USD_LATAM) are differentiated manually by cc."
    )


if __name__ == "__main__":
    main()
