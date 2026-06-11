"""MCP resources —— 領域知識層(資料語意字典 + 分析方法論)。

把「表的語意(單位 / 調整 / 代碼 / 滯後)」與「分析該怎麼做」放進協定層,讓接上
這個 MCP 的外部 agent 不必只靠 44 個 tool 的 docstring 拼湊。內容為英文(給全球
agent),透過 `@mcp.resource("uri")` 以 import side-effect 註冊(同 tools.py 模式)。

兩個 resource:
  - data://dictionary       —— 資料語意字典,按 domain 分節。
  - data://analysis-playbook —— analyze_stock playbook 的文件版(給不支援 prompts 的 client)。

字典每個欄位名都對照過 schema 真相(investor_db/db/models.py)與
stock-analysis-report/SKILL.md;不腦補欄位。
"""

from __future__ import annotations

from trading_agent_mcp.server import mcp

# ============================================================
# data://dictionary —— 資料語意字典
# ============================================================

_DATA_DICTIONARY = """\
# investor-db Data Dictionary

Semantic reference for the data served by this MCP. Read this before quoting numbers:
the tool docstrings tell you *how to call*; this tells you *what the numbers mean*, what
units they are in, and where they will trip you up.

## Global conventions (apply to every domain)

- **Coverage:** US equities + ADRs only. No non-US local listings, no crypto, no indices
  as tradable tickers (index *levels* are available as macro series — see Macro).
- **Not real-time:** everything is EOD-or-slower. No intraday quotes, no streaming.
- **No analyst estimates / no forecasts:** there are NO consensus EPS estimates, NO price
  targets you can trust as research (the one `analyst_target_price` field on the overview is
  a vendor passthrough, often null), NO earnings forecasts. Describe the present; do not invent
  a forecast or a target price.
- **Money is raw USD.** All monetary figures are the actual dollar amount, NOT thousands and
  NOT millions. `revenue = 391035000000` means $391.035B. Share counts are actual shares.
- **Dates** are `YYYY-MM-DD` ISO strings. Timestamps are ISO 8601 (often UTC, `...Z`).
- **null means "not available", never zero.** A missing value is unknown / not-covered.
  Do not coerce null to 0 in any sum, ratio, or average — drop it and say it is missing.
- **Always carry an as-of date.** Different domains have very different lag (technical = today,
  financials = last quarter, 13F = ~45+ days stale). Quote freshness, do not blend silently.
- Before quoting exact figures, you can call `get_data_coverage` for per-domain freshness.

---

## Domain: Companies (registry)

Which tools serve this domain: `list_companies`, `search_companies`, `get_company`.

- Identity + classification only: `ticker`, `cik`, `name`, `sector`, `industry`, `sic_code`,
  `exchange`, `is_active`, `first_seen`, `last_updated`. `country` is currently always null.
- `sector` / `industry` set the **interpretation thresholds** — a 15% margin is great for a
  retailer and weak for software. Resolve sector first, then judge everything relative to peers
  and relative to the company's own history (preferred) rather than absolute cutoffs.
- Pitfall: `search_companies` is for "I don't know the exact ticker"; `get_company` needs the
  exact ticker and 404s otherwise.

---

## Domain: Prices (daily / hourly)

Which tools serve this domain: `list_daily_prices`, `get_latest_price`, `list_hourly_prices`.

- Source: Alpha Vantage `TIME_SERIES_DAILY_ADJUSTED`. EOD, **T+1** (you see yesterday's close
  the next day), refreshed Mon-Sat. **5-year rolling window** for daily (newer listings shorter);
  hourly is 60-minute bars with a **60-day** rolling retention only.
- Daily fields: `ticker`, `date`, `open`, `high`, `low`, `close`, `volume`. Prices are USD.
- **Adjusted-close caveat (important):** the underlying `prices_daily` table stores an
  `adj_close` (split/dividend-adjusted) column, **but the price tools do NOT return it** — you
  only get raw `close`. To build a split/dividend-adjusted return series across a corporate
  action, pull `list_dividends` / `list_splits` and adjust yourself. Within a window with no
  split/large dividend, raw `close` is fine for returns and moving averages.
- Returns / moving averages / 52-week position: compute them **from one `list_daily_prices`
  response** (e.g. a year of bars). Do not call a separate tool per metric.
- Pitfall: hourly `dt` is a `timestamptz` (US-eastern wall time preserved) — pass ISO UTC
  bounds. Use daily for anything longer than ~60 days.

---

## Domain: Financials (income / balance / cash flow)

Which tools serve this domain: `get_income_statements`, `get_balance_sheets`,
`get_cash_flow_statements`, `get_latest_period`.

- Source: SEC XBRL filings, **USD-normalized** (multi-currency ADRs are already converted to
  USD; the row's `reporting_currency` tells you the native filing currency).
- **`fiscal_period` domain:** `Q1`, `Q2`, `Q3`, `Q4` (quarters) or `FY` (full year). Each row
  also carries `fiscal_year` and `period_end`. The `period` arg maps `"quarterly"` to
  `Q1..Q4` and `"annual"` to `FY`.
- **TTM (trailing twelve months) = sum of the 4 most recent *quarter* rows. NEVER mix an FY
  row into a TTM sum** (double counts). When computing TTM EPS, sum the 4 quarterly
  `eps_diluted` (or use `net_income / shares_diluted`).
- Income fields: `revenue`, `cost_of_revenue`, `gross_profit`, `operating_expenses`,
  `rd_expense`, `sga_expense`, `operating_income`, `interest_expense`, `tax_expense`,
  `net_income`, `eps_basic`, `eps_diluted`, `shares_basic`, `shares_diluted`. Use
  **`eps_diluted`** and **`shares_diluted`** for valuation; falling `shares_diluted` over time =
  buybacks.
- Balance fields: `cash_and_equivalents`, `short_term_investments`, `accounts_receivable`,
  `inventory`, `total_current_assets`, `ppe_net`, `goodwill`, `intangibles`, `total_assets`,
  `accounts_payable`, `short_term_debt`, `total_current_liabilities`, `long_term_debt`,
  `total_liabilities`, `total_equity`. Net debt =
  `long_term_debt + short_term_debt - cash_and_equivalents - short_term_investments`.
  Current ratio = `total_current_assets / total_current_liabilities`.
- Cash-flow fields: `operating_cash_flow`, `capex`, `free_cash_flow`, `dividends_paid`,
  `share_buybacks`, `financing_cash_flow`, `investing_cash_flow`, `net_change_in_cash`.
  **When `free_cash_flow` is null, compute it as `operating_cash_flow - |capex|`** (capex is
  stored as an outflow; take its absolute value).
- YoY growth: compare a quarter to the **same quarter last year** (e.g. Q3 vs prior Q3) to
  avoid seasonality — not quarter-over-quarter.
- Pitfall: loss-making companies have negative / meaningless P/E — fall back to P/S, EV/EBITDA,
  and revenue growth, and label P/E "N/A (loss)".

---

## Domain: Valuation snapshot (company_overview)

Which tools serve this domain: `get_overview` (and the bundled `get_objective_report`).

- Source: derived from Alpha Vantage OVERVIEW plus a self-computed market cap
  (latest `close` x `shares_diluted`), refreshed daily ~16:00 UTC.
- **`market_cap` covers ~7k of ~20k tickers. Missing = not-covered, NOT zero.** Never treat a
  null market cap as a tiny / zero-cap company.
- Absolute USD (nullable): `market_cap`, `shares_outstanding`, `ebitda`, `revenue_ttm`,
  `gross_profit_ttm`. Ratios (float): `pe_ratio`, `forward_pe`, `peg_ratio`, `price_to_book`,
  `price_to_sales_ttm`, `ev_to_ebitda`, `ev_to_revenue`. Per-share: `eps`, `diluted_eps_ttm`,
  `book_value`.
- **Decimals-not-percent fields:** `dividend_yield`, `profit_margin`, `operating_margin_ttm`,
  `return_on_assets_ttm`, `return_on_equity_ttm` are **0-1 decimals** — multiply by 100 for a
  percent (0.073 = 7.3%).
- Technicals: `beta`, `week_52_high`, `week_52_low`, `ma_50`, `ma_200`. `forward_pe <
  pe_ratio` implies the market prices in earnings growth.
- Pitfall: `get_overview` 404s for a ticker the overview ETL has not covered yet — that is
  "not covered", not "no such company".

---

## Domain: Insider trades (SEC Form 4)

Which tools serve this domain: `list_insider_trades` (single ticker),
`screen_insider_buys` (cross-market).

- Source: SEC Form 4. **SEC requires filing within 2 business days of the trade**, so this is
  near-current but still a short lag. Ingested daily.
- Fields: `transaction_date`, `insider_name`, `insider_title`, `transaction_code`, `shares`,
  `price_per_share` (USD), `total_value` (USD), `shares_owned_after`, `is_direct`.
- **`transaction_code` is the whole game — only `P` and `S` are tradable signals:**
  - `P` = open-market **purchase** (bullish, the strongest single-name signal).
  - `S` = open-market **sale** (weaker signal — often tax / diversification, read conservatively).
  - `A` = grant/award, `M` = option exercise, `G` = gift, `F` = shares withheld for tax, etc.
    **These are NOT discretionary trades — never count them as buy/sell signals.** Always
    filter to `transaction_code IN ('P','S')` before drawing a conclusion.
- `total_value` is the USD notional of the transaction (`shares x price_per_share`).
- **Cluster buying** (≈3+ distinct insiders buying `P` within ~90 days) is the strongest
  bullish read; buy signals outweigh sell signals; CEO/CFO carry more weight.

---

## Domain: Institutional holdings — 13F (institutional_holdings)

Which tools serve this domain: `list_13f_holders`, `list_13f_portfolio`,
`list_13f_top_buyers`, `list_13f_top_sellers`, `list_institutional_holders`,
`get_holders_breakdown`, plus the filer lookups `search_institutions` / `get_institution`.

- Source: first-party SEC 13F-HR. **Legal filing lag is ~45 days after quarter end**, so the
  newest `quarter_end` you see reflects positions as of *last* quarter's close — already weeks
  stale. **Always annotate 13F conclusions with the ~45-day lag.** Refreshed on a daily 1/7
  filer rotation.
- Fields: `filer_cik`, `filer_name`, `ticker`, `cusip`, `quarter_end`, `shares`,
  `market_value` (USD), `change_in_shares`, `change_type`.
- **`change_type` domain:** `new` / `increase` / `decrease` / `sold_out` (some endpoints
  surface the SEC labels `NEW` / `ADD` / `REDUCE` / `EXIT` — same meaning). Adders ≫ trimmers
  with positive net shares = bullish; two+ consecutive quarters of net adding = stronger.
- **13F is long US equity positions only** — no shorts, no options, no non-US holdings. A 13F
  drop does not necessarily mean a bearish bet (could be a sale, a hedge unwind, or a
  reallocation).
- `ticker` may be null in a filer's portfolio when the CUSIP maps to a non-US security or
  derivative not in our watchlist.

---

## Domain: Options (options_eod)

Which tools serve this domain: `get_options_chain`, `get_option_expirations`,
`get_option_contract_history`.

- Source: Alpha Vantage HISTORICAL_OPTIONS. **EOD for the prior trading day (T-1)** — one row
  is one OCC contract's end-of-day snapshot.
- Fields: `contract_id` (OCC), `date`, `underlying`, `expiration`, `strike`, `option_type`
  (`call`/`put`), `last`, `mark`, `bid`, `bid_size`, `ask`, `ask_size`, `volume`,
  `open_interest`, and the greeks `implied_volatility`, `delta`, `gamma`, `theta`, `vega`,
  `rho`.
- **Numeric fields come back as JSON strings** (e.g. `"200.0000"`, `"-0.019830"`) — cast to
  float before doing math; greeks can be negative. `null` = quote/greek missing; do not treat
  it as 0.
- Put/Call ratio = sum(put volume) / sum(call volume); ATM IV = `implied_volatility` of the
  near-month contract with `|delta| ≈ 0.5`. Interpret IV relative to the name's own history.
- **Low-liquidity caveat:** many ADRs have no options at all (treat the options lens as
  missing, not bearish), and thin contracts (tiny volume/OI) should not be over-read.
- Pitfall: `underlying` is the AV symbol and may not match `companies.ticker` exactly
  (BRK.B vs BRK-B). If empty, try the dot/dash variant.

---

## Domain: Corporate actions (dividends / splits)

Which tools serve this domain: `list_dividends`, `list_splits`.

- Source: Alpha Vantage, refreshed weekly (Sunday).
- Dividends: `ex_dividend_date`, `declaration_date`, `record_date`, `payment_date` (the last
  three are frequently null in the source), `amount` (per-share cash dividend, USD).
- Splits: `effective_date`, `split_factor` (= new shares / old shares; 2:1 forward = 2.0,
  1:10 reverse = 0.1).
- Primary use: adjust the raw daily price series across corporate actions (since the price
  tools do not expose `adj_close`), and read ex-div gaps.

---

## Domain: Earnings calendar (earnings_calendar)

Which tools serve this domain: `list_earnings` (single ticker),
`get_earnings_calendar` (cross-market scan).

- Source: Alpha Vantage, refreshed daily ~03:00 UTC.
- Fields: `report_date` (scheduled announcement date — a **vendor estimate**, can shift),
  `fiscal_date_ending`, `estimate_eps` (often null), `currency` (often null), `report_time`
  (`pre-market` / `post-market` / null).
- Use it to flag an imminent earnings date (swing traders avoid the gap). **There are no
  analyst EPS estimates to rely on** — `estimate_eps` is a sparse vendor passthrough.

---

## Domain: ETF (profile / holdings / sector weights)

Which tools serve this domain: `get_etf_profile`, `list_etf_holdings`,
`list_etfs_holding_ticker`, `list_etf_sectors`.

- Source: Alpha Vantage, monthly refresh, **fixed universe of 25 large ETFs** (not every ETF).
- Profile: `net_assets` (USD), `net_expense_ratio`, `portfolio_turnover`, `dividend_yield`,
  `inception_date`, `leveraged` (bool). **`net_expense_ratio` / `portfolio_turnover` /
  `dividend_yield` are 0-1 decimals**, not percents.
- Holdings: `etf_ticker`, `holding_symbol`, `description`, `weight` (**0-1 decimal** share of
  the ETF's net value, not a percent). `list_etfs_holding_ticker` reverses it (which ETFs hold
  a given symbol) — useful as passive-flow *context*, not a trading signal.
- Sector weights: `etf_ticker`, `sector` (GICS), `weight` (0-1 decimal, returned as a JSON
  string).

---

## Domain: Macro / commodity / index series (macro_series)

Which tools serve this domain: `list_macro_series` (catalog — call first),
`get_macro_series` (observations).

- **Units differ per series and the observation rows do NOT carry a unit.** Always read the
  `unit` field from `list_macro_series` for that `series_id` before interpreting a value
  (e.g. `percent`, `USD`, `index`).
- `series_id` is **case-sensitive** — use the exact value from the catalog (e.g. `CPI`,
  `TREASURY_YIELD_10YEAR`, `WTI`, `INDEX_SPX`, `INDEX_NDX`, `INDEX_VIX`).
- **Cadence:** economic + commodity series are **monthly**; the index series
  `INDEX_SPX` / `INDEX_NDX` / `INDEX_VIX` are **daily**. Observation fields: `series_id`,
  `date`, `value`.

---

## Two integrated views (when you want a shortcut)

- `get_analysis` — four-lens red/amber/green verdicts with a one-line summary per lens
  (interpreted). Caveat: `overall_summary` lists only bullish/bearish lenses; neutral lenses
  (and some down-graded bearish signals like heavy insider selling) are omitted — inspect each
  lens's `signals` yourself before reporting risk. `signals[].value` is a display string, not a
  machine number.
- `get_objective_report` — one bundled "data pack" (overview + recent statements + insider +
  13F + price summary + options summary + filing section titles), **no interpretation**. Use it
  to avoid 8 separate calls when you want to analyze the raw material yourself.
- Power tier only: `execute_readonly_sql` + `describe_table` for flexible aggregation across
  the ~25 tables.
"""


@mcp.resource("data://dictionary")
def data_dictionary() -> str:
    """Semantic data dictionary for the investor-db dataset.

    Field meanings, units, adjustment semantics, transaction codes, filing lags, and the
    null-vs-zero rule, organized by domain, with the tools that serve each domain. Read this
    before quoting numbers so you interpret them correctly.
    """
    return _DATA_DICTIONARY


# ============================================================
# data://analysis-playbook —— analyze_stock playbook 的文件版
# ============================================================
# 註:此為 prompts.py 之 analyze_stock 的 document 版本,內容刻意鏡像那條 playbook,
# 給不支援 MCP prompts 的 client 也能拿到方法論。維護時兩邊一起改。

_ANALYSIS_PLAYBOOK = """\
# Stock Analysis Playbook (investor-db)

A repeatable, tool-driven workflow for a simple price + financials read on one US ticker.
This is the document form of the `analyze_stock` prompt — use it when your client cannot
invoke MCP prompts. Respond in the user's language.

## Hard honesty rules (apply throughout)

- Every number carries an as-of date. Domains have different lag — do not blend silently.
- Missing data is flagged and **excluded from the conclusion**, never guessed or zero-filled.
- There are **no analyst estimates and no real-time quotes**: describe the current state, do
  NOT give a price target or a forecast.
- Prefer "relative to its own history" over absolute thresholds; thresholds vary by sector.
- For a loss-making company, drop P/E (label "N/A (loss)") and use P/S, EV/EBITDA, growth.

## Step 0 — Resolve

If the ticker is uncertain, `search_companies` first. Then `get_company` for
`sector` / `industry` — interpretation thresholds differ by industry.

## Step 1 — Valuation snapshot

`get_overview`: market cap, P/E family (`pe_ratio`, `forward_pe`, `peg_ratio`), margins.
Remember margins/yields/ROE are 0-1 decimals; a missing `market_cap` means **not-covered**,
not a zero-cap stock.

## Step 2 — Price state (ONE call)

`list_daily_prices` for ~1 year, then compute **everything from that single response**:
latest close + date, 1/3/6/12-month returns, 52-week position, 50/200-day MA alignment,
and volume vs its ~50-day average. Do not call a separate tool per metric.

## Step 3 — Financial trajectory

`get_income_statements(quarterly, 8)` + `get_cash_flow_statements(quarterly, 8)` +
`get_balance_sheets(quarterly, 4)`. Then:
- Revenue and EPS (`eps_diluted`) **YoY vs the same quarter last year** (avoid seasonality);
  is YoY accelerating across the last 4 quarters?
- Gross / operating margin trend (expanding vs compressing).
- FCF: use `free_cash_flow`, or `operating_cash_flow - |capex|` when it is null.
- Net debt and share-count change (falling `shares_diluted` = buybacks).
- **TTM = sum of the 4 most recent quarter rows; never mix an FY row.**

## Step 4 — Events

`list_earnings` for the next `report_date` (call it out if imminent — swing traders avoid the
gap) and `list_dividends` for recent cash dividends.

## Step 5 — Synthesize

Lead with one sentence: price state x fundamental direction x the single biggest risk. Then
back it with the numbers, each with its as-of date. Flag any missing data and exclude it from
the conclusion.

## Advanced modules (expand only on request or a clear signal)

- **Ownership / flows:** `list_insider_trades` (only `transaction_code` `P`/`S`; cluster buying
  = ≥3 distinct buyers); `list_13f_top_buyers` / `list_13f_top_sellers` for two consecutive
  quarters of net direction (annotate the ~45-day 13F lag).
- **Options:** `get_options_chain` for put/call ratio and ATM IV (cast the JSON-string numbers
  to float; skip illiquid contracts).
- **Macro:** `get_macro_series` for `INDEX_VIX` / `TREASURY_YIELD_10YEAR` as context.
"""


@mcp.resource("data://analysis-playbook")
def analysis_playbook() -> str:
    """Tool-driven workflow for a simple price + financials analysis of one ticker.

    Document form of the `analyze_stock` prompt, for clients that cannot invoke MCP prompts.
    Step 0 resolve -> Step 1 valuation -> Step 2 price state -> Step 3 financials -> Step 4
    events -> Step 5 synthesize, with the dataset's honesty rules baked in.
    """
    return _ANALYSIS_PLAYBOOK
