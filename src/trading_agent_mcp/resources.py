"""MCP resources —— 領域知識層(資料語意字典 + 分析方法論)。

把「表的語意(單位 / 調整 / 代碼 / 滯後)」與「分析該怎麼做」放進協定層,讓接上
這個 MCP 的外部 agent 不必只靠 47 個 tool 的 docstring 拼湊。內容為英文(給全球
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
- **Analyst data is limited to two vendor fields** (verified against prod 2026-06-11):
  `earnings_calendar.estimate_eps` (vendor consensus EPS for an upcoming report date,
  populated on ~3/4 of calendar entries) and the overview's `analyst_target_price`
  (populated on ~half of covered tickers, refreshed weekly). You MAY cite these two, always
  attributed as vendor estimates with their as-of date. There is NO full analyst dataset:
  no buy/hold/sell ratings, no analyst counts, no revenue forecasts, no estimate-revision
  history. Never invent your own forecast or price target.
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
  `exchange`, `is_active`, `status`, `delisted_at`, `first_seen`, `last_updated`. `country` is
  currently always null.
- `sector` / `industry` set the **interpretation thresholds** — a 15% margin is great for a
  retailer and weak for software. Resolve sector first, then judge everything relative to peers
  and relative to the company's own history (preferred) rather than absolute cutoffs.
- **Delisting (`status` + `delisted_at`):** `status` is `'active'` or `'delisted'`;
  `delisted_at` is the DATE delisting was detected (null while active). **A stock that simply
  stopped getting fresh prices is NOT necessarily delisted — judge delisting by `status` /
  `delisted_at`, not by a price gap.** `is_active` is just `status == 'active'` (kept for
  backward compatibility); prefer `status` / `delisted_at` for the precise read.
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
- Absolute USD (nullable): `market_cap`, `shares_outstanding`, `float_shares`,
  `free_float_market_cap`, `ebitda`, `revenue_ttm`, `gross_profit_ttm`. Ratios (float):
  `float_pct` (free-float fraction, 0-1), `pe_ratio`, `forward_pe`, `peg_ratio`,
  `price_to_book`, `price_to_sales_ttm`, `ev_to_ebitda`, `ev_to_revenue`. Per-share: `eps`,
  `diluted_eps_ttm`, `book_value`.
- **Decimals-not-percent fields:** `float_pct`, `dividend_yield`, `profit_margin`,
  `operating_margin_ttm`, `return_on_assets_ttm`, `return_on_equity_ttm` are **0-1 decimals** —
  multiply by 100 for a percent (0.073 = 7.3%; float_pct 0.75 = 75% of shares freely traded).
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
- Pitfall: the `ticker` argument is the AV symbol and may not match `companies.ticker` exactly
  (BRK.B vs BRK-B). If empty, try the dot/dash variant. (The data row still carries an
  `underlying` field — same value, just the response column name.)

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
- Use it to flag an imminent earnings date (swing traders avoid the gap). `estimate_eps`
  IS a usable vendor consensus EPS for the upcoming report (populated on ~3/4 of entries) —
  cite it with attribution. There are NO ratings, revenue forecasts, or estimate history.

---

## Domain: Earnings call transcripts (earnings_call_transcripts / earnings_call_segments)

Which tools serve this domain: `get_earnings_transcript`.

- Source: Alpha Vantage `EARNINGS_CALL_TRANSCRIPT`, **available ~T+1 after the call, quarterly**.
  Stored two-layer: the queryable segment rows live in Postgres; the raw JSON is archived to R2
  (re-parse insurance) — you only ever read the PG rows.
- **Coverage skews to mid/large-cap.** Many small-caps have no transcript at all; the tool
  honestly errors for those rather than inventing one.
- **Tombstone semantics:** internally a `(ticker, quarter)` row with `segments = 0` means "we
  asked AV and there is NO transcript for that quarter" (a negative cache so we do not re-fetch).
  **Tombstones are never exposed** — `get_earnings_transcript` and the transcript list only
  surface quarters with `segments > 0`.
- Segment fields: `seq` (0-based order within the call), `speaker`, `speaker_title`, `content`,
  `sentiment`. A call is typically **~60-80 segments, ~40-50k characters of text total**.
- **`sentiment` is a per-segment score from AV's own model (a vendor metric, NOT computed by this
  platform)** — treat it as a soft signal, not a precise number. There is no platform-side
  sentiment on transcripts.
- `quarter` is the AV **calendar** quarter string, e.g. `2025Q4`. Omitting it returns the latest
  available quarter.
- Pitfall: a call's full text is large — `get_earnings_transcript` defaults to `limit=40`
  segments to protect context; page with `offset` for the rest, or pass `speaker` to read just
  one person (e.g. the CEO). `segments_total` is the whole-call length regardless of paging.

---

## Domain: News + sentiment (news_articles / news_ticker_sentiment)

Which tools serve this domain: `get_company_news` (single ticker), `get_market_news`
(market-wide).

- **Source: Alpha Vantage `NEWS_SENTIMENT` — a VENDOR AGGREGATION, not first-hand SEC data.**
  This is the one clearly third-party domain here; frame it as such. The aggregator mixes in
  low-grade sources, so **source quality varies** and relevance filtering is essential.
- **Vendor source grading via `relevance`:** `news_ticker_sentiment.relevance` (0-1) is how
  related an article is to a given ticker. **Keep `min_relevance >= 0.5` for signal** — below
  that it is mostly keyword-spam noise. `get_company_news` defaults to 0.5.
- **Sentiment fields are AV's model scores (vendor metric, not ours).** Each article has an
  `overall_sentiment` (NUMERIC, market-wide) + `overall_label` (AV's text label, e.g.
  `Bullish` / `Somewhat-Bullish` / `Neutral` / `Bearish`); each ticker link additionally has a
  per-ticker `sentiment` + `label`. Do not present these as platform-computed.
- Article fields: `url` (UNIQUE — this IS the link to the publisher; AV gives only title +
  `summary`, never full body text), `title`, `summary`, `source`, `source_domain`,
  `published_at`, `topics` (JSONB `[{topic, relevance}]`). `get_market_news` can filter by
  `topic` (e.g. `earnings`, `ipo`, `mergers_and_acquisitions`, `financial_markets`,
  `technology`).
- **Retention: 12 months** (older articles are trimmed weekly). Do not expect multi-year news
  history.
- **Cadence: refreshed every 4 hours — labelled by fetch time, NOT real-time.** The platform
  stays EOD-positioned; the news block is "as of its last fetch", never sold as a live feed.
  This is the 消息面 (news/sentiment) lens: query the DB here first, and use web search only as a
  supplement.

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
- **Ordering:** `get_macro_series` returns **newest-first by default** (`order='desc'`), so
  `limit=1` gives you the latest reading (e.g. the current VIX). Pass `order='asc'` when you
  need an oldest-first time series for charting or a moving-average calculation.

---

## Domain: Market movers (market_movers)

Which tools serve this domain: `get_market_movers`.

- Source: Alpha Vantage `TOP_GAINERS_LOSERS`. One market-wide snapshot **per trading day,
  refreshed after the close (EOD, not real-time)**, with three top-20 boards.
- **Three `category` boards:** `gainers` (highest `change_pct` of the day), `losers` (lowest
  `change_pct`), `most_active` (highest `volume`, regardless of direction). Each board has
  `rank` 1-20.
- Per-row fields: `rank`, `ticker`, `price` (USD), `change_amount` (day's price move vs the
  prior close, USD), `change_pct`, `volume`. **`change_pct` is a percent number (5.23 = +5.23%),
  NOT a 0-1 decimal** — do not multiply by 100 again.
- Calling with no `date` returns the latest available trading day (the normal path); passing a
  date with no data 404s (omit the date to get the latest).

---

## Domain: IPO calendar (ipo_calendar)

Which tools serve this domain: `get_ipo_calendar`.

- Source: Alpha Vantage `IPO_CALENDAR`, refreshed daily. One row per upcoming / recent IPO.
- Fields: `symbol`, `ipo_date` (scheduled listing date — a **vendor estimate, can shift**),
  `name`, `price_range_low` / `price_range_high` (offering price band, USD), `currency`
  (ISO 4217), `exchange`.
- **`null` (or 0) price range means not-yet-priced**, not a $0 IPO — say "not priced yet".
- With no date arguments the tool defaults to a **today .. +90 day** window (upcoming IPOs);
  pass `from_date` / `to_date` to widen or look back.

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
- **No real-time quotes; analyst data limited to two vendor fields** (`estimate_eps` on the
  earnings calendar, `analyst_target_price` on the overview — cite with attribution): describe
  the current state, never invent your own price target or forecast. For a market-expectation
  read, prefer the options-implied move / IV. First-party SEC data is from EDGAR, not scraped.
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


# ============================================================
# data://report-template —— self-contained HTML report design system
# ============================================================
# 給 build_stock_report prompt 用。一份「設計系統 + 組裝指南」(非死板填空模板),
# 讓 agent 用真實數字組出單一自包含 HTML(預設零外部依賴)。版型/配色蒸餾自
# 三隻研究 agent 的結論(InvestSkill 配方、sell-side note 結構、可及性與第一手
# 資料 moat 框架)與本專案既有的紅綠燈方法論。維護時與 build_stock_report 一起改。

_REPORT_TEMPLATE = """\
# investor-db HTML Stock-Report Template

A self-contained HTML **design system** for the `build_stock_report` workflow. Do NOT treat it
as fill-in-the-blanks: copy the `<style>` block verbatim, then assemble the body from the
component patterns using REAL values you fetched from the tools. Output **one self-contained
`.html` file** — inline CSS, no build step, **no external assets by default** (works offline).

## Output rules (load-bearing)

- **Conclusion-first:** the verdict card is the first thing in the body.
- **Numbers in monospace:** every figure sits in a `.num` span (JetBrains Mono) for alignment.
- **Color is never the only signal:** every 🟢🟡🔴⚪ pairs an emoji/word with the CSS color
  (colorblind- and grayscale/print-safe; keep WCAG 4.5:1 contrast).
- **As-of on everything:** put `As of <date>` on each data block; never blend freshness silently.
- **First-party data is the hero:** order the lens panels Insider -> Institutional -> Options ->
  Financials; prices/macro are supporting context, not the headline.
- **Missing lens -> ⚪, dropped from the denominator,** and the verdict states "based on N/4 lenses".
- **No invented price target / no forecast.** Use the options-implied move / IV / skew as the
  honest market-expectation read instead.
- **Charts are EXPECTED, not optional** (a wall of text is a failure). Minimum set: (1) price +
  MA50 + MA200 + volume trend; (2) options IV term-structure; (3) IV skew curve; (4) open-interest
  by strike (calls vs puts); (5) ownership trend (13F holders + insider net). Render with
  **Chart.js 4.4.7 via CDN** (the one allowed external dep); inline-SVG sparklines need no dep. See §1b.
- **Depth is EXPECTED per section** (a thin draft is the #1 failure mode): a FULL valuation table
  (P/E, fwd P/E, P/S, EV/Sales, EV/EBITDA, P/B, PEG, P/FCF + FCF yield, div yield, analyst target),
  a SHORT-INTEREST & FLOAT block (shares out, float, % insiders, % institutions, short shares,
  short % of float, days-to-cover, trend) + a squeeze checklist, and an OPTIONS-ANALYTICS block
  (term structure, IV rank, skew, OI/volume by strike, max pain, expected move). See §3.
- **Compute, don't omit:** EV = mkt cap + total debt − cash; EV/Sales = EV / TTM revenue; max pain
  = argmin over strikes of total option payout; expected move ≈ price × ATM_IV × √(DTE/365).
  Loss-makers: P/E & EV/EBITDA → "N/M", lead with P/S + EV/Sales + a Rule-of-40 line.

## 1. Page skeleton (copy verbatim; fill {{...}})

```html
<!DOCTYPE html>
<html lang="{{LANG}}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{TICKER}} — Stock Report</title>
<!-- Charts: the one allowed external dependency (UMD build auto-registers everything). -->
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.js"></script>
<!-- OPTIONAL web fonts (omit for a fully offline file; system fonts are the fallback): -->
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#f6f7f9; --card:#fff; --ink:#0f172a; --muted:#64748b; --line:#e2e8f0;
    --green:#16a34a; --green-bg:#dcfce7; --amber:#d97706; --amber-bg:#fef3c7;
    --red:#dc2626; --red-bg:#fee2e2; --gray:#94a3b8; --gray-bg:#f1f5f9;
    --accent:#3949ab; --accent-soft:#eef1fb;
    --sans:"Inter",-apple-system,"Segoe UI",system-ui,"Microsoft JhengHei","PingFang TC","Noto Sans TC",sans-serif;
    --mono:"JetBrains Mono",ui-monospace,"Cascadia Code",Consolas,monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);line-height:1.6;font-size:15px;-webkit-font-smoothing:antialiased}
  .wrap{max-width:1080px;margin:0 auto;padding:0 20px 80px}
  header{background:linear-gradient(135deg,#1a237e 0%,#3949ab 55%,#5c6bc0 100%);color:#fff;padding:34px 0 26px;margin-bottom:24px}
  header .wrap{padding-top:0;padding-bottom:0}
  h1{margin:0 0 6px;font-size:26px;letter-spacing:-.4px}
  .asof{display:inline-block;margin-top:8px;font-family:var(--mono);font-size:12px;background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.25);border-radius:7px;padding:5px 10px}
  .prov{opacity:.9;font-size:12.5px;margin-top:8px;max-width:760px}
  h2{font-size:20px;margin:32px 0 12px;padding-bottom:7px;border-bottom:2px solid var(--line)}
  .num{font-family:var(--mono)}
  a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
  .dot{display:inline-block;width:11px;height:11px;border-radius:50%;vertical-align:middle;margin-right:5px}
  .g{background:var(--green)} .y{background:var(--amber)} .r{background:var(--red)} .w{background:var(--gray)}
  /* BLUF verdict card */
  .verdict{border:1px solid var(--line);border-radius:14px;padding:16px 20px;margin:18px 0;box-shadow:0 1px 3px rgba(15,23,42,.05)}
  .verdict.vg{background:var(--green-bg)} .verdict.vy{background:var(--amber-bg)} .verdict.vr{background:var(--red-bg)}
  .verdict .head{font-weight:700;font-size:17px;display:block;margin-bottom:4px}
  .lights{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}
  .light{font-size:12.5px;font-family:var(--mono);padding:4px 10px;border-radius:7px;border:1px solid var(--line);background:#fff}
  /* KPI dashboard */
  .kpi{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:14px 0}
  .k{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:9px 11px}
  .k .lab{font-size:11px;color:var(--muted);display:block}
  .k .val{font-family:var(--mono);font-size:16px;font-weight:600;margin-top:2px}
  .k .val.g{color:var(--green)} .k .val.r{color:var(--red)} .k .val.y{color:var(--amber)}
  /* lens / aspect block */
  .aspect{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 18px;margin:12px 0}
  .aspect .t{font-weight:700;font-size:15px;margin-bottom:4px}
  .aspect ul{margin:6px 0 0;padding-left:20px} .aspect li{margin:3px 0}
  /* callouts */
  .warn{background:#fff7ed;border:1px solid #fed7aa;border-radius:9px;padding:9px 13px;font-size:13px;margin:9px 0;color:#9a3412}
  .intro{background:var(--accent-soft);border:1px solid #d7ddf5;border-left:4px solid var(--accent);border-radius:10px;padding:12px 16px}
  /* comparison table (peers) */
  table{width:100%;border-collapse:collapse;margin:14px 0;font-size:13.5px;background:var(--card);border-radius:12px;overflow:hidden;border:1px solid var(--line)}
  th,td{padding:9px 12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
  th{background:var(--accent);color:#fff;font-weight:600} tr:last-child td{border-bottom:none}
  td:first-child{font-weight:600;color:#334155;white-space:nowrap}
  .cmp td:nth-child(2){background:#fffbeb} .cmp td:nth-child(3){background:#f0fdf4}
  .rt{text-align:right;font-variant-numeric:tabular-nums}
  /* charts + layout */
  .chart-card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px 18px;margin:14px 0}
  .chart-card h3{font:600 14px/1.3 var(--sans);margin:0 0 10px}
  .chart-wrap{position:relative;width:100%;height:300px} .chart-wrap.tall{height:380px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  .chk{font-family:var(--mono);font-size:13px;line-height:1.9}
  @media(max-width:720px){.grid2{grid-template-columns:1fr}}
  /* sources + disclaimer */
  .sources{font-size:12.5px;line-height:2}
  .disc{font-size:12px;color:var(--muted);margin-top:22px;border-top:1px solid var(--line);padding-top:14px}
  @media(max-width:720px){.kpi{grid-template-columns:repeat(2,1fr)}h1{font-size:21px}}
  @media print{body{background:#fff}header,.verdict.vg,.verdict.vy,.verdict.vr,.k,th,.cmp td,.warn,.intro{-webkit-print-color-adjust:exact;print-color-adjust:exact}}
</style>
</head>
<body>
  <header><div class="wrap">
    <h1>{{TICKER}} — {{COMPANY NAME}}</h1>
    <div>{{exchange}} · {{sector}} · {{currency}} · mkt cap {{$X.XB}}</div>
    <span class="asof">As of {{EOD date}} · EOD/filing-based — built for fundamental, ownership &amp; positioning analysis (multi-day to multi-quarter), not a real-time / intraday execution tool</span>
    <div class="prov">Sourced &amp; normalized directly from SEC EDGAR filings + primary feeds — not scraped or aggregated.</div>
  </div></header>
  <div class="wrap">
    <!-- body assembled from the patterns below -->
  </div>
</body>
</html>
```

## 1b. Charts (Chart.js 4.4.7 — the one allowed CDN dep)

Set defaults once, then drop a `<canvas>` inside a `.chart-wrap` per chart; data is inlined as JS
arrays. `animation:false` + `devicePixelRatio:2` make them print cleanly. The price chart is the
full pattern; the other four follow the same shape (dual-axis / line / diverging bar).

```html
<script>
Chart.defaults.font.family="'Inter',system-ui,sans-serif";Chart.defaults.color="#475569";
Chart.defaults.animation=false;Chart.defaults.devicePixelRatio=2;Chart.defaults.maintainAspectRatio=false;
const GRID="rgba(15,23,42,.07)",BLUE="#3949ab",AMBER="#d97706",VIOLET="#7c3aed",GREEN="#16a34a",RED="#dc2626",CYAN="#0891b2";
function mk(id,cfg){const e=document.getElementById(id);if(e)try{new Chart(e,cfg)}catch(x){e.outerHTML='<p>chart error</p>'}}

// 1) PRICE + MA50 + MA200 + VOLUME — dual axis; vol bars behind (order:3), vol axis squashed low.
mk('priceChart',{type:'line',data:{labels:DATES,datasets:[
 {type:'bar',label:'Vol',data:VOL,yAxisID:'yV',backgroundColor:'rgba(57,73,171,.18)',order:3},
 {label:'Close',data:CLOSE,borderColor:BLUE,borderWidth:1.8,pointRadius:0,order:0},
 {label:'MA50',data:MA50,borderColor:AMBER,borderWidth:1.2,pointRadius:0,borderDash:[4,3],order:1},
 {label:'MA200',data:MA200,borderColor:VIOLET,borderWidth:1.2,pointRadius:0,borderDash:[2,3],order:2}]},
 options:{interaction:{mode:'index',intersect:false},scales:{x:{grid:{display:false},ticks:{maxTicksLimit:8}},
 y:{grid:{color:GRID},ticks:{callback:v=>'$'+v}},
 yV:{position:'right',grid:{drawOnChartArea:false},beginAtZero:true,max:Math.max.apply(null,VOL)*3.2,ticks:{callback:v=>(v/1e6)+'M'}}}}});

// 2) IV TERM STRUCTURE — line: labels=expiries, data=ATM IV %. (CYAN, fill:true, tension:.25)
// 3) IV SKEW — line: labels=strikes, two datasets callIV(GREEN)/putIV(RED), spanGaps:true.
// 4) OI BY STRIKE — diverging: type:'bar', indexAxis:'y', put data .map(v=>-v), BOTH scales stacked:true,
//    strip the minus in x ticks/tooltip (Math.abs). calls GREEN right, puts RED left.
// 5) OWNERSHIP — type:'bar' holders on yLeft + a type:'line' insider-net on yRight (dual-axis like #1).
</script>
```
Tiny KPI sparkline = dependency-free inline SVG `<polyline>` (x=i/(n-1)*W, y=H-((v-min)/(max-min))*H).
Print-safe: `addEventListener('beforeprint',()=>{for(const i in Chart.instances)Chart.instances[i].resize()})`.

## 2. Component patterns (fill with real values)

**Verdict card (BLUF, first in body)** — pick `vg`/`vy`/`vr` by overall light:
```html
<div class="verdict vy">
  <span class="head"><span class="dot y"></span>🟡 Neutral-bullish · based on 4/4 lenses</span>
  One sentence: strongest bullish reason + biggest risk. No price target.
</div>
<div class="lights">
  <span class="light"><span class="dot g"></span>Fundamentals · bullish</span>
  <span class="light"><span class="dot y"></span>Ownership · mixed</span>
  <span class="light"><span class="dot g"></span>Technical · bullish</span>
  <span class="light"><span class="dot w"></span>Options · ⚪ no data (dropped from denominator)</span>
</div>
```
Per-lens signal = bullish / neutral / bearish (+ a confidence word). **No Buy/Sell action.**

**KPI dashboard** (numbers in `.num`/mono; color the `.val` by read):
```html
<div class="kpi">
  <div class="k"><span class="lab">Price (As of {{date}})</span><span class="val">{{$167.34}}</span></div>
  <div class="k"><span class="lab">6M return</span><span class="val g">{{+460%}}</span></div>
  <div class="k"><span class="lab">Fwd P/E</span><span class="val r">{{84}}</span></div>
  <div class="k"><span class="lab">Next earnings</span><span class="val">{{8/6}}</span></div>
</div>
```

**Lens block** (🟢🟡🔴⚪ dot + label; Insider/Institutional/Options are the hero panels):
```html
<div class="aspect">
  <div class="t"><span class="dot g"></span>Ownership — Insider (Form 4) &amp; Institutional (13F)</div>
  <ul>
    <li>Cluster buying: {{N}} distinct insiders incl. {{CEO/CFO}} bought within ~90d (P only). As of {{Form 4 disclosed date}}.</li>
    <li>13F: adders {{vs}} trimmers, net {{+X.XM}} shares, {{new/exit}} positions. As of {{quarter-end}}, filed up to ~45-day lag.</li>
  </ul>
</div>
```
Options lens: lead with IV rank / 25-delta **skew** / put-call / max pain as an EOD positioning read.

**Valuation table (full — never just one or two ratios):**
```html
<table><tr><th>Multiple</th><th>Current</th><th>Read</th></tr>
  <tr><td>P/E (TTM)</td><td class="num rt">{{N/M if loss}}</td><td>...</td></tr>
  <tr><td>Forward P/E</td><td class="num rt">{{84}}</td><td>...</td></tr>
  <tr><td>P/S · EV/Sales</td><td class="num rt">{{26.5 · 27.6}}</td><td>...</td></tr>
  <tr><td>EV/EBITDA</td><td class="num rt">{{N/M}}</td><td>...</td></tr>
  <tr><td>P/B · PEG</td><td class="num rt">{{12.1 · 0.78}}</td><td>...</td></tr>
  <tr><td>P/FCF · FCF yield</td><td class="num rt">{{N/M}}</td><td>...</td></tr>
  <tr><td>Div yield · analyst target</td><td class="num rt">{{— · $151}}</td><td>...</td></tr>
</table>
```
Loss-maker: P/E & EV/EBITDA → "N/M"; lead P/S + EV/Sales; add a Rule-of-40 line (rev growth % + FCF/op margin %).

**Short-interest & float block:**
```html
<div class="grid2">
  <table><tr><th>Share structure</th><th>Value</th></tr>
    <tr><td>Shares outstanding</td><td class="num rt">{{80.24M}}</td></tr>
    <tr><td>Float (% of shares)</td><td class="num rt">{{76.08M (94.8%)}}</td></tr>
    <tr><td>% insiders / % institutions</td><td class="num rt">{{5.1% / 72%}}</td></tr></table>
  <table><tr><th>Short interest</th><th>Value</th></tr>
    <tr><td>Short shares (prior)</td><td class="num rt">{{10.04M (9.33M)}}</td></tr>
    <tr><td>Short % of float</td><td class="num rt">{{13.2%}}</td></tr>
    <tr><td>Days-to-cover</td><td class="num rt">{{0.84}}</td></tr>
    <tr><td>As-of (settlement)</td><td class="num rt">{{date}}</td></tr></table>
</div>
<div class="chk">Squeeze check — short%float {{✓/✗}} · days-to-cover {{✓/✗}} · trend {{↑/↓}} → {{verdict}}</div>
```
Short interest & float are web-sourced; stamp the settlement date. Borrow-fee / utilization are paid → omit honestly, never fabricate.

**Options-analytics block (3 charts + a metrics table):**
```html
<div class="grid2">
  <div class="chart-card"><h3>ATM IV term structure</h3><div class="chart-wrap"><canvas id="termChart"></canvas></div></div>
  <div class="chart-card"><h3>IV skew ({{expiry}})</h3><div class="chart-wrap"><canvas id="skewChart"></canvas></div></div>
</div>
<div class="chart-card"><h3>Open interest by strike — call vs put</h3><div class="chart-wrap tall"><canvas id="oiChart"></canvas></div></div>
<table><tr><th>Options</th><th>Value</th><th>Read</th></tr>
  <tr><td>P/C (vol · OI)</td><td class="num rt">{{0.39 · 1.16}}</td><td>...</td></tr>
  <tr><td>ATM IV / IV rank</td><td class="num rt">{{125%}}</td><td>...</td></tr>
  <tr><td>Implied move (~30d)</td><td class="num rt">{{±35% (±$59)}}</td><td>price×IV×√(DTE/365)</td></tr>
  <tr><td>Max pain</td><td class="num rt">{{$170}}</td><td>argmin payout</td></tr>
</table>
```

**Financials trend table (multi-quarter — the story, not one line):**
```html
<table><tr><th>Qtr</th><th>Revenue</th><th>YoY</th><th>Gross %</th><th>Op %</th><th>EPS</th></tr>
  <tr><td>{{Q1'26}}</td><td class="num rt">{{$151.1M}}</td><td class="num rt">{{+132%}}</td><td class="num rt">...</td><td class="num rt">...</td><td class="num rt">...</td></tr>
  <tr><td>TTM</td><td class="num rt">...</td>...</tr>
</table>
```
Pair with FCF (TTM), net debt / net cash, current ratio, and — for loss-makers — cash runway.

**Caveat / data-gap box:**
```html
<div class="warn">⚠️ {{e.g. DB shows quarter-end cash; verify post-quarter raises via web before any runway claim.}}</div>
```

**Comparison table (only when peers given)** — col 2/3 auto-tint per ticker:
```html
<table class="cmp"><tr><th>Axis</th><th>{{TICKER_A}}</th><th>{{TICKER_B}}</th></tr>
  <tr><td>Revenue YoY</td><td class="num">{{+132%}}</td><td class="num">{{+26%}}</td></tr>
</table>
```

**Recent news (web overlay — keep SEPARATE from the cited first-party core):**
```html
<h2>Recent news (web; may post-date our EOD data)</h2>
<div class="intro">Each item: headline — source, date, one-line sentiment, link. Used to verify
DB anomalies (cash runway, anomalous Form 4, missing earnings date). Not part of the lens scoring.</div>
```

**Sources + freshness + disclaimer (footer):**
```html
<h2>Sources &amp; freshness</h2>
<div class="sources">Price as-of {{date}} (T+1 EOD) · financials {{last filing}} · 13F {{quarter-end}} (~45-day lag) · options {{EOD date}}. First-party from SEC EDGAR; valuation/prices via Alpha Vantage.</div>
<div class="disc">For informational/research purposes only. <b>Not investment advice.</b> Data is end-of-day and filing-based, not real-time; past performance is not indicative of future results.</div>
```

## 3. Assembly order

header → verdict card → lights row → KPI dashboard → **price + MA + volume chart** → **Insider →
Institutional (+ ownership-trend chart) → Options (term + skew + OI charts, analytics table) →
Financials (trend table)** → Valuation table → Short-interest & float block → Technical context →
events (next earnings, dividends) → Recent news (web overlay, separate) → Sources &amp; freshness →
disclaimer. Charts are mandatory (§1b); each section is a FULL block (§3), not one line; top-3
risks inline, minor detail to an appendix; every claim traces to a number shown on the page.
"""


@mcp.resource("data://report-template")
def report_template() -> str:
    """Self-contained HTML design system + assembly guide for the stock report.

    Used by the `build_stock_report` prompt. Copy the `<style>` verbatim and assemble the body
    from the documented component patterns with real values: BLUF verdict card, traffic-light
    lens row, KPI dashboard, first-party hero lens blocks (Form 4 / 13F / options), comparison
    table, a separated web-news section, and a sources/disclaimer footer. Inter + JetBrains Mono
    (numbers in mono), cobalt theme, colorblind/print-safe, mobile + print CSS, zero external
    dependencies by default (Chart.js optional).
    """
    return _REPORT_TEMPLATE
