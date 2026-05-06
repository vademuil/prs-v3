"""
Steam Publisher Revenue Calculator + Pricing Recommender
========================================================

Streamlit-приложение, которое для заданного Steam AppID:
  1. Тянет региональные цены через Steam Store API.
  2. Применяет inclusive-VAT по таблице Steam tax FAQ.
  3. Вычитает комиссию дистрибьютора.
  4. Конвертирует всё в USD.
  5. Дедуплицирует по Steam-валютам (включая разделение USD на тиры
     USD / USD_CIS / USD_SASIA / USD_MENA / USD_LATAM).
  6. Группирует валюты по пакетам (ROW / ASIA / CN / RU-CIS / LATAM / MENA),
     для каждого пакета выбирает базовую валюту, и рекомендует ПОДНЯТЬ
     publisher USD у остальных валют до уровня базы (raise-only).
  7. Из целевого publisher USD считает обратно retail USD c учётом VAT и
     комиссии дистрибьютора и применяет психологическое округление к .99.

Запуск:
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
    "orange":     "#FF7F42",   # каждое изменение цены
    "pink":       "#FF3895",   # большой разрыв (>15%)
}
# Полупрозрачные тинты для подсветки строк (чтобы текст оставался читаемым)
ROW_TINT_ORANGE = "rgba(255, 127, 66, 0.20)"
ROW_TINT_PINK   = "rgba(255, 56, 149, 0.20)"


def inject_css() -> None:
    """Подгружаем Poppins и кастомизируем стили Streamlit."""
    css = """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700&display=swap');

    html, body, [class*="css"], [class*="st-"],
    button, input, select, textarea,
    .stMarkdown, .stDataFrame, .stTable {
        font-family: 'Poppins', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
    }

    /* Главный заголовок и подзаголовки */
    h1, h2, h3, h4 {
        font-family: 'Poppins', sans-serif !important;
        font-weight: 600 !important;
        color: #1A1A1A !important;
    }

    /* Primary-кнопка */
    .stButton > button[kind="primary"] {
        background-color: #4600FF !important;
        color: #FFFFFF !important;
        border: none !important;
        border-radius: 8px !important;
        font-weight: 600 !important;
        padding: 10px 24px !important;
        transition: all 0.15s ease;
    }
    .stButton > button[kind="primary"]:hover {
        background-color: #3700CC !important;
        box-shadow: 0 4px 12px rgba(70, 0, 255, 0.25);
        transform: translateY(-1px);
    }
    .stButton > button[kind="primary"]:active {
        transform: translateY(0);
    }

    /* Secondary-кнопки (download) */
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

    /* Табы */
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
        border-bottom: 1px solid #E5E5E5;
    }
    .stTabs [data-baseweb="tab"] {
        font-weight: 500 !important;
        color: #707070 !important;
    }
    .stTabs [aria-selected="true"] {
        color: #4600FF !important;
    }
    .stTabs [data-baseweb="tab-highlight"] {
        background-color: #4600FF !important;
    }

    /* Expanders */
    .streamlit-expanderHeader,
    [data-testid="stExpander"] summary {
        font-weight: 500 !important;
    }

    /* Цветные точки в легенде */
    .legend-dot {
        display: inline-block;
        width: 14px;
        height: 14px;
        border-radius: 3px;
        vertical-align: -2px;
        margin-right: 6px;
    }

    /* Скрываем дефолтный заголовок Streamlit */
    [data-testid="stHeader"] {
        background-color: transparent;
    }

    /* Прижимаем контент чуть ближе к верху */
    .block-container {
        padding-top: 2rem !important;
    }
    </style>
    """
    st.markdown(css, unsafe_allow_html=True)


def render_logo() -> None:
    """Логотип в шапке. Ищется как logo.svg рядом со скриптом."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "logo.svg"),
        os.path.join(here, "Black.svg"),
        "logo.svg",
    ]
    logo_path = next((p for p in candidates if os.path.exists(p)), None)
    if not logo_path:
        return  # graceful — без логотипа просто не рисуем

    with open(logo_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")

    st.markdown(
        f'''
        <div style="display: flex; align-items: center; gap: 16px; margin-bottom: 8px;">
            <img src="data:image/svg+xml;base64,{b64}"
                 style="height: 56px; width: auto;" alt="logo"/>
        </div>
        ''',
        unsafe_allow_html=True,
    )

# ----------------------------------------------------------------------------
# VAT-таблица (inclusive). Источник:
# https://partner.steamgames.com/doc/finance/taxfaq (раздел Current Tax Rates)
# Снимок на май 2026.
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

# Доп. страны без inclusive-VAT, у которых на Steam своя валюта или
# особый USD-тир. VAT = 0.
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
    "CN": "China",
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
# USD-тиры. Steam Store API возвращает currency="USD" для нескольких разных
# ценовых тиров — различаем их по cc вручную.
# ----------------------------------------------------------------------------

USD_TIER_BY_CC: dict[str, str] = {
    # CIS USD-тир
    "BY": "USD_CIS",
    "MD": "USD_CIS",
    "RU": "USD_CIS",   # если Steam ответил USD (после ухода RUB)
    "UA": "USD_CIS",
    "KZ": "USD_CIS",
    "UZ": "USD_CIS",

    # South Asia USD-тир
    "BD": "USD_SASIA",

    # MENA USD-тир
    "MA": "USD_MENA",
    "EG": "USD_MENA",
    "KW": "USD_MENA",
    "QA": "USD_MENA",
    "TR": "USD_MENA",  # если Steam перевёл в USD
    "SA": "USD_MENA",  # если Steam ответил USD вместо SAR

    # LATAM USD-тир
    "AR": "USD_LATAM",

    # Default USD (US, CA, остальное) — без явной мапы → "USD"
}

# ----------------------------------------------------------------------------
# Справочник Steam-валют → пакет, override VAT (если нужен), display name.
# package: ROW | ASIA | CN_ONLY | RU_CIS | LATAM | MENA
# vat_override: если задан — используется вместо VAT представительной страны.
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

    # CN_ONLY: только CNY
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

# Базовая валюта пакета
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
    """Steam Store API всегда /100, включая JPY/KRW."""
    if amount_minor is None:
        return None
    try:
        return float(amount_minor) / 100.0
    except (TypeError, ValueError):
        return None


def relabel_currency(cc: str, currency: str) -> str:
    """USD → USD_CIS / USD_SASIA / USD_MENA / USD_LATAM по cc."""
    if currency != "USD":
        return currency
    return USD_TIER_BY_CC.get(cc, "USD")


def floor_to_99(x: float) -> float:
    """
    Психологическое округление вниз к ближайшему N.99.
    Примеры:
      50.24 → 49.99
      50.99 → 50.99 (уже на границе)
      51.00 → 50.99
      9.50  → 8.99
      0.50  → 0.50  (для значений <1 не трогаем)
    """
    if x is None:
        return None
    if x < 1:
        return round(x, 2)
    n = int(x)  # floor для положительных
    boundary = n + 0.99
    if x >= boundary - 1e-9:
        return round(boundary, 2)
    if n >= 1:
        return round(n - 1 + 0.99, 2)
    return round(x, 2)


# ----------------------------------------------------------------------------
# Build per-country detailed table (Mode A — текущая логика)
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
        "Регион": f"{cc} — {country_name}",
        "cc": cc,
        "Валюта": "—",
        "Цена в локальной валюте": None,
        "Цена в USD по курсу": None,
        "VAT %": f"{vat_rate * 100:.1f}%" if vat_rate > 0 else "0%",
        "Цена в USD без VAT": None,
        "Доход издателя (локальная)": None,
        "Доход издателя (USD)": None,
        "Note": "",
    }
    if not price_info:
        base["Note"] = "no price (free / not for sale)"
        return base

    currency = price_info.get("currency") or "—"
    local_price = steam_minor_to_major(price_info.get("final"))
    if local_price is None or local_price <= 0:
        base["Note"] = "no price"
        base["Валюта"] = currency
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
        "Валюта": currency,
        "Цена в локальной валюте": round(local_price, 2),
        "Цена в USD по курсу": round(usd_gross, 2) if usd_gross is not None else None,
        "Цена в USD без VAT": round(usd_ex_vat, 2) if usd_ex_vat is not None else None,
        "Доход издателя (локальная)": round(publisher_local, 2),
        "Доход издателя (USD)": round(publisher_usd, 2) if publisher_usd is not None else None,
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
# Pricing recommendations: дедуп, группировка, raise-only, ψ-rounding
# ----------------------------------------------------------------------------

def deduplicate_by_currency_tier(
    per_country_results: dict[str, dict | None],
    countries: dict[str, tuple[float, str]],
) -> dict[str, dict]:
    """
    Возвращает {tier: {cc, country_name, currency, local_price, vat_country}}
    Берём первую страну, в которой увидели данный тир (или для USD-тиров —
    первую матчащую по USD_TIER_BY_CC).
    """
    tiers: dict[str, dict] = {}
    # Проходим в детерминированном порядке (USD-тиры приоритетнее, потом остальные)
    # Чтобы для EUR взять условно DE (а не AT/BE), отсортируем по cc.
    for cc in sorted(per_country_results.keys()):
        data = per_country_results.get(cc)
        if not data:
            continue
        currency = data.get("currency")
        if not currency:
            continue
        tier = relabel_currency(cc, currency)
        if tier in tiers:
            continue  # уже есть представитель
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
    """USD-тиры конвертим по USD (rate=1). Остальные — по своей валюте."""
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
    Обратная формула. FX сокращается, поэтому только VAT и dist_fee.
        retail_usd = target_pub_usd * (1 + vat) / (1 - dist_fee)
    """
    return target_pub_usd * (1 + vat) / (1 - distributor_fee_pct / 100.0)


def build_recommendations(
    per_country_results: dict[str, dict | None],
    fx_rates: dict[str, float],
    distributor_fee_pct: float,
) -> dict[str, dict]:
    """
    Возвращает {package: {"base_tier", "base_pub_usd", "rows": [...] }}.
    """
    countries = all_countries()
    deduped = deduplicate_by_currency_tier(per_country_results, countries)

    # Считаем publisher_usd для каждого тира
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

    # Группируем по пакетам
    by_package: dict[str, list[dict]] = {pkg: [] for pkg in PACKAGE_ORDER}
    for tier, item in enriched.items():
        pkg = item["package"]
        if pkg in by_package:
            by_package[pkg].append(item)

    # На каждый пакет: находим базу, считаем target и рекомендации
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
                # gap_pct = насколько current ниже target, в долях от target
                # > 0 если нужно поднимать; ≤ 0 если уже выше или равен базе
                if target_pub_usd > 0:
                    gap_pct = (target_pub_usd - current_pub) / target_pub_usd
                else:
                    gap_pct = 0.0

            # Если цену не меняем (delta == 0 либо None) — не трогаем retail.
            # Только когда реально поднимаем (delta > 0) — пересчитываем и ψ-округляем.
            EPS = 1e-6
            should_change_price = (
                delta is not None and delta > EPS
            )

            if should_change_price:
                rec_retail_usd_raw = reverse_to_retail_usd(
                    rec_pub, item["vat"], distributor_fee_pct
                )
                rec_retail_usd_psy = floor_to_99(rec_retail_usd_raw)
                # local при ψ-округл retail
                if item["fx"]:
                    rec_retail_local = rec_retail_usd_psy * item["fx"]
                else:
                    rec_retail_local = None
            else:
                # Никаких изменений — рекомендация = текущая retail.
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

        # Сортируем: база сверху, далее по delta_pub_usd убыв.
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
    Подсветка строки рекомендации:
      • orange — цена изменена (delta > 0)
      • pink — цена изменена И gap > 15% (большой разрыв)
      • без цвета — цена не меняется (база или уже выше базы)
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


def render_recommendations(rec: dict[str, dict], distributor_fee_pct: float) -> None:
    st.markdown(
        f"**Логика:** в каждом пакете выбирается базовая валюта, и для остальных "
        f"валют publisher USD поднимается до уровня базы (только вверх). "
        f"Если цена уже даёт ≥ базы — не трогаем (retail остаётся как есть). "
        f"Если поднимаем — пересчитываем retail с учётом VAT и комиссии "
        f"дистрибьютора ({distributor_fee_pct}%), затем ψ-округляем до .99."
    )
    st.markdown(
        f'''
        <div style="display:flex; gap:24px; align-items:center; font-size:14px; margin: 4px 0 12px;">
            <span><span class="legend-dot" style="background:{BRAND['orange']};"></span>рекомендуем поднять цену</span>
            <span><span class="legend-dot" style="background:{BRAND['pink']};"></span>большой разрыв (&gt;15% от базы)</span>
            <span><span class="legend-dot" style="background:{BRAND['green']};"></span>без изменений</span>
        </div>
        ''',
        unsafe_allow_html=True,
    )

    for pkg in PACKAGE_ORDER:
        block = rec.get(pkg, {})
        rows = block.get("rows", [])
        base_tier = block.get("base_tier")
        base_pub_usd = block.get("base_pub_usd")

        title = PACKAGE_DISPLAY.get(pkg, pkg)
        if not rows:
            with st.expander(f"{title} — нет данных", expanded=False):
                st.info("Не удалось получить цены ни в одной валюте этого пакета.")
            continue

        if base_pub_usd is None:
            header = f"{title} · база: {base_tier} (нет цены)"
        else:
            header = f"{title} · база: {base_tier} · target publisher USD: ${base_pub_usd:.2f}"

        with st.expander(header, expanded=(pkg == "ROW")):
            df = pd.DataFrame(rows)

            # Сохраняем служебные поля для стайлера
            df["_is_changed"] = df["is_changed"]
            df["_gap_pct"] = df["gap_pct"]

            display = df.rename(columns={
                "tier_label": "Tier",
                "country": "Представитель",
                "currency_raw": "Steam currency",
                "vat_pct": "VAT %",
                "current_local_price": "Текущая локальная",
                "current_retail_usd": "Текущая USD retail",
                "current_pub_usd": "Текущий pub USD",
                "target_pub_usd": "Target pub USD",
                "rec_pub_usd": "Rec pub USD",
                "delta_pub_usd": "Δ pub USD",
                "gap_pct_str": "Разрыв",
                "rec_retail_usd_raw": "Rec retail USD (raw)",
                "rec_retail_usd_psy": "Rec retail USD (.99)",
                "rec_retail_local": "Rec retail local",
            })

            cols = [
                "Tier", "Steam currency", "Представитель", "VAT %",
                "Текущая локальная", "Текущая USD retail",
                "Текущий pub USD", "Target pub USD", "Разрыв",
                "Rec pub USD", "Δ pub USD",
                "Rec retail USD (raw)", "Rec retail USD (.99)",
                "Rec retail local",
            ]

            # Стайлер применяем по полному display, потом отбираем колонки
            styled = (
                display[cols + ["_is_changed", "_gap_pct"]]
                .style
                .apply(_row_style, axis=1)
                .hide(subset=["_is_changed", "_gap_pct"], axis="columns")
                .format({
                    "Текущая локальная":      "{:.2f}",
                    "Текущая USD retail":     "${:.2f}",
                    "Текущий pub USD":        "${:.2f}",
                    "Target pub USD":         "${:.2f}",
                    "Rec pub USD":            "${:.2f}",
                    "Δ pub USD":              "{:+.2f}",
                    "Rec retail USD (raw)":   "${:.2f}",
                    "Rec retail USD (.99)":   "${:.2f}",
                    "Rec retail local":       "{:.2f}",
                }, na_rep="—")
            )

            st.dataframe(styled, use_container_width=True, hide_index=True)

            # ---- Кандидаты на исключение из дистрибуции (gap > 5%) ----
            removal_candidates = [
                r for r in rows
                if r["is_changed"]
                and r["gap_pct"] is not None
                and r["gap_pct"] > 0.05
            ]
            if removal_candidates:
                st.markdown("**Кандидаты на исключение из дистрибуции** (если поднять цену не вариант):")
                items_html = []
                for r in removal_candidates:
                    gap = r["gap_pct"] * 100
                    color = BRAND["pink"] if r["gap_pct"] > 0.15 else BRAND["orange"]
                    items_html.append(
                        f'<li><span class="legend-dot" style="background:{color};"></span>'
                        f'<b>{r["tier"]}</b> — разрыв <b>{gap:+.1f}%</b>, '
                        f'текущий pub USD ${r["current_pub_usd"]}, нужно ${r["rec_pub_usd"]} '
                        f'(или исключить из дистрибуции)</li>'
                    )
                st.markdown(
                    f'<ul style="margin: 4px 0 0 0; padding-left: 18px; line-height: 1.7;">{"".join(items_html)}</ul>',
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    f'<div style="color:{BRAND["green"]}; font-weight:500; margin-top:8px;">'
                    f'✓ Все валюты пакета в пределах 5% от базы — кандидатов на исключение нет.'
                    f'</div>',
                    unsafe_allow_html=True,
                )


def main() -> None:
    st.set_page_config(
        page_title="Steam Publisher Revenue Calculator",
        page_icon="💰",
        layout="wide",
    )

    inject_css()
    render_logo()

    st.title("Steam Publisher Revenue Calculator")
    st.caption(
        "Считает доход издателя по региональным ценам Steam с учётом VAT и "
        "комиссии дистрибьютора, плюс рекомендует целевые цены по пакетам "
        "(ROW / Asia / CN / RU-CIS / LATAM / MENA) для защиты от cross-border-арбитража."
    )

    with st.sidebar:
        st.header("Параметры")
        appid = st.text_input("Steam AppID", value="730", help="Например, 730 = CS2").strip()
        distributor_fee = st.number_input(
            "Комиссия дистрибьютора, %",
            min_value=0.0, max_value=99.0, value=20.0, step=0.5,
        )
        st.markdown("---")
        run = st.button("Рассчитать", type="primary", use_container_width=True)

    if not run:
        st.info("👈 Заполни параметры слева и нажми **Рассчитать**.")
        st.stop()

    if not appid.isdigit():
        st.error("AppID должен быть числом, например `730`.")
        st.stop()

    meta = fetch_app_meta(appid)
    app_name = (meta or {}).get("name") or f"AppID {appid}"

    progress_bar = st.progress(0.0, text="Получаем цены из Steam Store API…")
    df, fx_rates, fx_last, raw_results = build_pricing_table(
        appid=appid,
        distributor_fee_pct=distributor_fee,
        progress_cb=lambda p: progress_bar.progress(p, text=f"Получаем цены… {int(p*100)}%"),
    )
    progress_bar.empty()

    rec = build_recommendations(raw_results, fx_rates, distributor_fee)

    st.subheader(app_name)
    st.markdown(
        f"**AppID:** `{appid}` · "
        f"**Комиссия дистрибьютора:** {distributor_fee}% · "
        f"**FX update:** {fx_last or 'unknown'}"
    )

    tab_detail, tab_rec = st.tabs([
        "📊 Все регионы (детально)",
        "🎯 Рекомендации по пакетам",
    ])

    # ---- Tab 1: detailed per-country ----
    with tab_detail:
        valid_df = df[df["Note"] == ""].drop(columns=["Note", "cc"]).reset_index(drop=True)
        skipped_df = df[df["Note"] != ""][["Регион", "Валюта", "Note"]].reset_index(drop=True)

        st.dataframe(
            valid_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Цена в локальной валюте":      st.column_config.NumberColumn(format="%.2f"),
                "Цена в USD по курсу":          st.column_config.NumberColumn(format="$%.2f"),
                "Цена в USD без VAT":           st.column_config.NumberColumn(format="$%.2f"),
                "Доход издателя (локальная)":   st.column_config.NumberColumn(format="%.2f"),
                "Доход издателя (USD)":         st.column_config.NumberColumn(format="$%.2f"),
            },
        )

        with st.expander(f"Регионы без данных ({len(skipped_df)})"):
            if skipped_df.empty:
                st.write("Цены получены везде ✓")
            else:
                st.dataframe(skipped_df, use_container_width=True, hide_index=True)

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        st.download_button(
            "💾 Скачать CSV (все регионы)",
            data=valid_df.to_csv(index=False).encode("utf-8"),
            file_name=f"steam_pricing_{appid}_{ts}.csv",
            mime="text/csv",
        )

    # ---- Tab 2: recommendations ----
    with tab_rec:
        render_recommendations(rec, distributor_fee)

        # Объединённый CSV-экспорт всех рекомендаций
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
            "💾 Скачать CSV (рекомендации)",
            data=rec_df.to_csv(index=False).encode("utf-8"),
            file_name=f"steam_pricing_rec_{appid}_{ts}.csv",
            mime="text/csv",
        )

    st.markdown("---")
    st.caption(
        "**Источники:** "
        "[Steam Store API](https://store.steampowered.com/api/appdetails) · "
        "[Steam tax FAQ](https://partner.steamgames.com/doc/finance/taxfaq) · "
        "[open.er-api.com](https://open.er-api.com) (FX). "
        "Steam revenue share (30%) НЕ учитывается — расчёт под продажу через дистрибьютора (CD-keys). "
        "USD-тиры (USD/USD_CIS/USD_SASIA/USD_MENA/USD_LATAM) различаются вручную по cc."
    )


if __name__ == "__main__":
    main()
