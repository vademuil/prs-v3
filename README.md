# Steam Publisher Revenue Calculator + Pricing Recommender

Streamlit-приложение, которое считает доход издателя по региональным ценам Steam с учётом VAT и комиссии дистрибьютора, и **рекомендует целевые цены по 6 пакетам** (ROW / Asia / CN / RU-CIS / LATAM / MENA) для защиты от cross-border-арбитража.

## Что делает

На вход:
- **AppID Steam** (например, `730` для CS2) — получает фактические текущие цены через Steam Store API.
- **Комиссия дистрибьютора, %** — процент, удерживаемый реселлером/площадкой.

На выход — две вкладки:

### 📊 Все регионы (детально)

Таблица по ~70 странам с фактическими ценами Steam, посчитанными VAT-вычетами и доходом издателя:

| Регион | Валюта | Цена в локальной | Цена в USD по курсу | VAT % | Цена в USD без VAT | Доход издателя (локальная) | Доход издателя (USD) |

### 🎯 Рекомендации по пакетам

6 expanders (по одному на пакет). Внутри каждого — таблица валют пакета с:
- Текущим publisher USD
- Целевым publisher USD (= база пакета)
- Рекомендованным publisher USD (raise-only: max(current, target))
- Δ pub USD (на сколько поднимаем)
- Рекомендованным retail USD: raw + после ψ-округления к .99
- Рекомендованным retail в локальной валюте

Обе таблицы экспортируются в CSV.

## Логика расчёта

### Per-country (вкладка «Все регионы»)

```
local_price        = Steam Store API → final / 100
local_price_ex_vat = local_price / (1 + vat_rate)        # только если VAT > 0 в Steam tax FAQ
publisher_local    = local_price_ex_vat * (1 - distributor_fee/100)
publisher_usd      = publisher_local / fx_rate(currency, USD)
```

### Pricing recommendation (вкладка «Рекомендации»)

1. **Дедуп по Steam-валютам.** Берём первую страну для каждой валюты. EUR — одна строка, не повторяется на DE/FR/IT/etc.
2. **Разделение USD на тиры по cc.** Steam Store API всегда отвечает `currency: "USD"`, но цена для US != BY != BD != MA. Различаем вручную:
   - `USD` — глобальный (US, CA по дефолту)
   - `USD_CIS` — BY, MD, RU/UA если перевели в USD, KZ, UZ
   - `USD_SASIA` — BD
   - `USD_MENA` — MA, EG, KW, QA, TR, SA (если в USD)
   - `USD_LATAM` — AR
3. **Override VAT для EUR = 21%.** Чтобы не считать 19/21/22/24/27% по разным EU-странам, используем единый рейт.
4. **Группировка по пакетам:**
   - **ROW** — все валюты, не попавшие в специальные пакеты (USD, EUR, GBP, AUD, CAD, CHF, NOK, NZD, PLN, ZAR, CZK, ...)
   - **ASIA** — JPY, KRW, TWD, HKD, SGD, MYR, THB, IDR, PHP, VND, INR, USD_SASIA
   - **CN_ONLY** — CNY
   - **RU_CIS** — RUB, UAH, KZT, USD_CIS
   - **LATAM** — BRL, MXN, ARS, CLP, COP, PEN, UYU, CRC, USD_LATAM
   - **MENA** — ILS, AED, SAR, QAR, KWD, TRY, USD_MENA
5. **База пакета:** ROW → EUR · ASIA → USD_SASIA · CN_ONLY → CNY · RU_CIS → RUB · LATAM → BRL · MENA → ILS.
6. **Raise-only.** Для каждой не-базовой валюты:
   `rec_publisher_usd = max(current_publisher_usd, base_publisher_usd)`.
   Если valyuta уже даёт больше базы — оставляем как есть.
7. **Обратная формула:** `rec_retail_usd = rec_publisher_usd × (1 + vat) / (1 − dist_fee)`. FX cancels out.
8. **ψ-округление:** floor к ближайшему N.99: `50.24 → 49.99`, `51.00 → 50.99`, `9.50 → 8.99`.

## Источники данных

- **Цены:** [Steam Store API](https://store.steampowered.com/api/appdetails) — публичный, без ключей.
- **VAT:** [Steam tax FAQ → Current Tax Rates](https://partner.steamgames.com/doc/finance/taxfaq) — захардкожен снимок (62 страны). Обновлять вручную при изменении.
- **FX:** [open.er-api.com](https://open.er-api.com) — бесплатно, без ключей, ECB-based, обновляется ежедневно.

## Запуск локально

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Откроется на `http://localhost:8501`.

## Деплой на Streamlit Cloud

1. Залить `streamlit_app.py`, `requirements.txt`, `README.md` в публичный GitHub-репо.
2. Зайти на [share.streamlit.io](https://share.streamlit.io), connect GitHub, выбрать репо.
3. Указать main file: `streamlit_app.py`.
4. Deploy. Готово.

## Известные ограничения / TODO для v2

- **Steam Store API rate limit** — 200 запросов / 5 минут с одного IP. Кешируем по `(appid, cc)` на 1 час, но при шквале AppID из разных IP можно словить 429. Можно добавить экспоненциальный backoff.
- **Mode B (Base USD)** — простая FX-конверсия. Не учитывает [Steam suggested pricing tiers](https://partner.steamgames.com/doc/store/pricing) с psychological rounding (`.99`, `.49`). Для точных рекомендаций нужно подгружать таблицу Valve (login-gated) или хардкодить снимок.
- **Country → currency map в Mode B** — захардкожен. Steam периодически меняет регионы (RU/AR/TR перевели на USD). Снимок на май 2026.
- **VAT-таблица** — снимок Steam tax FAQ, обновлять вручную. Можно добавить периодический скрейп страницы tax FAQ + diff.
- **Steam revenue share (30%)** — не вычитается. Расчёт оптимизирован под продажу CD-keys через дистрибьютора, где Valve не получает свою долю. Если нужен Mode "Steam Store sales", добавить отдельный flag и вычитать 30% после VAT.
- **Discount (текущая скидка на Steam)** — отображаем `final_formatted` цену, т.е. со скидкой если она активна. Добавить опциональный toggle "use original price (initial)".
- **Steam China (XC код)** — отдельная экосистема (Perfect World), сюда не включена. CN в выборке = глобальный Steam с CNY-pricing.

## Структура файлов

```
streamlit_app.py          # вся логика и UI в одном файле
requirements.txt          # зависимости
README.md                 # этот файл
logo.svg                  # логотип в шапке (опционально — без него работает)
.streamlit/config.toml    # тема: primaryColor #4600FF, белый фон, Poppins
```

## Брендинг

- **Фон:** `#FFFFFF`
- **Primary (кнопки, акценты):** `#4600FF`
- **Шрифт:** Poppins (подгружается с Google Fonts)
- **Подсветка строк рекомендаций:**
  - Orange `#FF7F42` (тинт ~20%) — рекомендуем поднять цену
  - Pink `#FF3895` (тинт ~20%) — большой разрыв (>15% от базы)
  - Green `#3DD070` — без изменений / OK

Если хочешь поменять палитру — правь словарь `BRAND` в `streamlit_app.py` и `[theme]` в `.streamlit/config.toml`.
