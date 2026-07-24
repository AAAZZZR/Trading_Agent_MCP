"""MCP prompts —— 分析方法論模板,把「該怎麼分析」做進協定層。

三個 prompt(模板內容英文,給全球 agent;每條第一行都要求 agent 用使用者語言回覆):
  - analyze_stock(ticker)             —— 簡單股價 + 財務分析(主管定稿的 playbook)。
  - analyze_stock_full(ticker)        —— 四面向完整報告(從 SKILL.md 蒸餾)。
  - compare_stocks(ticker_a, b)       —— 兩檔對照。

透過 `@mcp.prompt` 以 import side-effect 註冊(同 tools.py 模式)。所有 tool 名與欄位語意
都對照過 tools.py 與字典,缺資料的誠實處理規則貫穿三條。
"""

from __future__ import annotations

from trading_agent_mcp.server import mcp

# ============================================================
# analyze_stock —— 簡單股價 / 財務分析(playbook)
# ============================================================
# 註:此模板與 resources.py 的 data://analysis-playbook 同源,維護時兩邊一起改。


@mcp.prompt
def analyze_stock(ticker: str) -> str:
    """Simple price + financials analysis of one US ticker, as a step-by-step tool workflow.

    Produces a conclusion-first read of valuation, price state, and financial trajectory using
    the minimum set of tool calls, with the dataset's honesty rules (as-of dates, no forecasts,
    missing-data excluded) baked in.

    Args:
        ticker: US stock symbol, e.g. AAPL.
    """
    return f"""\
Respond in the user's language. Analyze **{ticker}** with a simple price + financials read.

Follow this workflow. Be conclusion-first, attach an as-of date to every number, flag any
missing data and EXCLUDE it from the conclusion, and never invent your own price target or
forecast (no real-time quotes; analyst data is limited to two vendor fields —
`estimate_eps` on the earnings calendar and `analyst_target_price` on the overview, which
you may cite with attribution). For a market-expectation read, prefer the options-implied
move / IV over any invented target. First-party SEC data is from EDGAR, not scraped.

**Step 0 — Resolve.** If the ticker is uncertain, call `search_companies` first. Then
`get_company` for `sector` / `industry` — interpretation thresholds differ by industry.

**Step 1 — Valuation snapshot.** `get_overview`: market cap, P/E family (`pe_ratio`,
`forward_pe`, `peg_ratio`), margins. Margins / yields / ROE are 0-1 decimals (x100 for %).
A missing `market_cap` means **not-covered**, not a zero-cap stock.

**Step 2 — Price state (ONE call).** `list_daily_prices` for ~1 year, then compute
**everything from that single response**: latest close + date, 1/3/6/12-month returns,
52-week position, 50/200-day MA alignment, and volume vs its ~50-day average. Do not call a
separate tool for each metric.

**Step 3 — Financial trajectory.** `get_income_statements(quarterly, 8)` +
`get_cash_flow_statements(quarterly, 8)` + `get_balance_sheets(quarterly, 4)`:
- Revenue and EPS (`eps_diluted`) **YoY vs the same quarter last year** (avoid seasonality);
  is YoY accelerating across the last 4 quarters?
- Gross / operating margin trend (expanding vs compressing).
- FCF: use `free_cash_flow`, or `operating_cash_flow - |capex|` when it is null.
- Net debt and share-count change (falling `shares_diluted` = buybacks).
- **TTM = sum of the 4 most recent quarter rows; never mix an FY row.**

**Step 4 — Events.** `list_earnings` for the next `report_date` (say so explicitly if it is
imminent) and `list_dividends` for recent cash dividends.

**Step 5 — Synthesize.** Lead with one sentence: price state x fundamental direction x the
single biggest risk. Then back it with the numbers, each with its as-of date. For a
loss-making company drop P/E (label "N/A (loss)") and use P/S, EV/EBITDA, growth instead.
Prefer "relative to its own history" over absolute thresholds.

**Advanced modules — expand only if asked or the signal is obvious:**
- Ownership/flows: `list_insider_trades` (only `transaction_code` `P`/`S`; cluster buying =
  ≥3 distinct buyers); `list_13f_top_buyers` / `list_13f_top_sellers` for two consecutive
  quarters of net direction (annotate the ~45-day 13F lag).
- Options: `get_options_chain` for put/call ratio and ATM IV (cast JSON-string numbers to
  float; skip illiquid contracts).
- Macro: `get_macro_series` for `INDEX_VIX` / `TREASURY_YIELD_10YEAR` as context.
"""


# ============================================================
# analyze_stock_full —— 四面向完整報告(蒸餾自 SKILL.md)
# ============================================================


@mcp.prompt
def analyze_stock_full(ticker: str) -> str:
    """Full four-lens report (fundamentals / ownership / technical / options) with traffic-light scoring.

    Distilled from the stock-analysis-report methodology: red/amber/green per lens, honest
    handling of missing lenses (drop from the denominator, mark blank, say "based on N/4"),
    the ~45-day 13F lag note, cluster-buying detection, and IV reading. Tool-call-first so it
    runs without a pro tier; mentions execute_readonly_sql for pro-tier aggregation.

    Args:
        ticker: US stock symbol, e.g. AAPL.
    """
    return f"""\
Respond in the user's language. Produce a conclusion-first four-lens report on **{ticker}**.

Use 🟢 bullish/healthy, 🟡 neutral/watch, 🔴 bearish/risk, ⚪ data missing. Honest rules:
a lens with no data is marked ⚪, **dropped from the denominator** (never scored 0), and you
always state "based on N/4 lenses". No forecasts, no price target — describe the present. Data is
EOD/filing-based (not real-time) — fine for fundamental/ownership/positioning; stamp each lens's
as-of. **First-party SEC data (Form 4 / 13F / options) is the differentiated core, sourced from
EDGAR, not scraped** — give it the most weight and visual prominence.

Order: lead with the **first-party, differentiated lenses** — Ownership (who is buying: Form 4 +
13F) -> Options (market-implied expectation) -> then Fundamentals (what is this company) ->
Technical (price state), then events + macro context. (Price/macro are context, not the headline.)

**Lens 1 — Fundamentals.** `get_income_statements` + `get_cash_flow_statements` +
`get_balance_sheets` (quarterly, ~8) and `get_overview`. Cover: growth (revenue/EPS YoY vs the
**same quarter last year**, accelerating?), profitability (gross / operating margin trend +
`return_on_equity_ttm`), valuation (`pe_ratio` vs `forward_pe`, `peg_ratio`, `ev_to_ebitda`;
loss-makers -> P/S + EV/EBITDA, label P/E "N/A (loss)"), balance-sheet health (net debt,
current ratio), and FCF (`free_cash_flow`, or `operating_cash_flow - |capex|` when null).
**TTM = sum of the 4 most recent quarter rows; never mix an FY row.**

**Lens 2 — Ownership (the differentiated lens).** Insider: `list_insider_trades` filtered to
`transaction_code IN ('P','S')` only (P=open-market buy, S=sell; A/M/G/F are grants/exercises
/gifts/tax — NOT signals). **Cluster buying = ≥3 distinct insiders buying within ~90 days =
strongest bullish read**; buys outweigh sells. Institutions: `list_13f_top_buyers` /
`list_13f_top_sellers` and `list_13f_holders` — adders ≫ trimmers with positive net shares is
bullish, two+ consecutive quarters of net adding is stronger. **Always annotate the ~45-day
13F filing lag** (you are seeing last quarter's positions).

**Lens 3 — Technical.** From one `list_daily_prices` (~1 year): close vs 50/200-day MA
alignment (close>MA50>MA200 = strong uptrend), 52-week position
`(close - week_52_low)/(week_52_high - week_52_low)`, 1/3/6-month momentum, and volume vs
~50-day average. `get_overview` `beta` for volatility.

**Lens 4 — Options (differentiated; many ADRs have none -> ⚪).** `get_option_expirations`
then `get_options_chain`. Put/Call ratio = put volume / call volume (<0.7 call-led/bullish,
>1.3 put-led/hedging). ATM IV = near-month `|delta|≈0.5` `implied_volatility`, read vs the
name's own history (high IV often pre-earnings -> beware IV crush). **Cast the JSON-string
numeric fields to float; ignore very thin contracts (mark "insufficient sample").**

**Events + macro.** `list_earnings` for the next `report_date` (flag if imminent),
`list_dividends`, and `get_macro_series` `INDEX_VIX` / `TREASURY_YIELD_10YEAR` as context.

**News & management tone (optional).** For the 消息面 (news) read use `get_company_news`
(vendor-aggregated, keep `min_relevance >= 0.5`; sentiment is AV's model, not ours). For
management tone / guidance, `get_earnings_transcript` gives the latest call (page with `offset`
or filter `speaker` to the CEO; transcript coverage skews to mid/large-cap — say so if absent).

**Synthesize.** One-line overall verdict naming the strongest bullish reason + the biggest
risk, the per-lens light row, and "based on N/4 lenses" with any ⚪ lens named. **Instead of a
price target, cite the options-implied move / IV / skew** as the market-expectation read. Close
with a one-line **"what would change this view"** (the catalyst or risk the thesis hinges on).

**Pro-tier shortcut (optional).** If `execute_readonly_sql` is available you can aggregate
directly instead of pulling rows. Examples:
- Insider buy/sell tally (last 6 months):
  `SELECT COUNT(*) FILTER (WHERE transaction_code='P') buys,
          COUNT(*) FILTER (WHERE transaction_code='S') sells,
          COUNT(DISTINCT insider_name) FILTER (WHERE transaction_code='P') distinct_buyers
   FROM insider_trades WHERE ticker='{ticker}'
     AND transaction_date >= CURRENT_DATE - INTERVAL '6 months'
     AND transaction_code IN ('P','S');`
- Latest-quarter 13F net flow:
  `SELECT COUNT(*) FILTER (WHERE change_type IN ('new','increase')) adding,
          COUNT(*) FILTER (WHERE change_type IN ('decrease','sold_out')) trimming,
          SUM(change_in_shares) net_share_change
   FROM institutional_holdings WHERE ticker='{ticker}'
     AND quarter_end=(SELECT MAX(quarter_end) FROM institutional_holdings WHERE ticker='{ticker}');`
"""


# ============================================================
# compare_stocks —— 兩檔對照
# ============================================================


@mcp.prompt
def compare_stocks(ticker_a: str, ticker_b: str) -> str:
    """Side-by-side comparison of two US tickers on growth, margins, valuation, FCF, and momentum.

    Runs the short Step 1-3 read on each, emits a comparison table, and closes with one sentence
    on who leads on which axis plus the shared risk. Same honesty rules (as-of dates,
    missing-data excluded, no forecasts).

    Args:
        ticker_a: First US stock symbol, e.g. AAPL.
        ticker_b: Second US stock symbol, e.g. MSFT.
    """
    return f"""\
Respond in the user's language. Compare **{ticker_a}** vs **{ticker_b}** head to head.

For EACH ticker run the short read (Steps 1-3), reusing one response per domain:
1. `get_overview` — market cap, `pe_ratio` / `forward_pe` / `peg_ratio`, margins
   (0-1 decimals, x100 for %; missing `market_cap` = not-covered, not zero).
2. `list_daily_prices` (~1 year) — from that single response: latest close, 1/3/6/12-month
   returns, 52-week position, 50/200-day MA alignment.
3. `get_income_statements` + `get_cash_flow_statements` (quarterly, ~8) — revenue & EPS
   (`eps_diluted`) YoY **vs the same quarter last year**, gross/operating margin trend, and
   FCF (`free_cash_flow`, or `operating_cash_flow - |capex|` when null). TTM = sum of the 4
   most recent quarter rows, never an FY row.

Then emit a **comparison table** with one row per axis and a column per ticker:
Revenue growth (YoY) | Operating margin | P/E (forward) | PEG | FCF (latest TTM) |
Price momentum (3/6-month) | 52-week position. Put the as-of date on each cell's domain.

Honesty rules: flag any missing cell as "n/a" and exclude it from the verdict; for a
loss-making name drop P/E and compare on P/S + growth; no price targets or forecasts (cite the
options-implied move, not an invented target). First-party SEC data (Form 4 / 13F / options) from
EDGAR is the differentiated core.

Close with **one sentence**: who leads on which axis (growth / profitability / valuation /
FCF / momentum) and the **shared risk** both face.
"""


# ============================================================
# build_stock_report —— self-contained HTML report (four-lens + web overlay)
# ============================================================
# 把 analyze_stock_full 的四面向方法論帶進 HTML 輸出,並新增兩件事:
#   (1) 一等的 web 新聞 overlay(用 agent 自己的 web search,無則退回 get_company_news),
#   (2) 用 data://report-template resource render 成單一自包含 HTML。
# 第一手資料(Form 4 / 13F / 期權 greeks)當招牌面板、誠實標 as-of、非實時定位、
# 不臆造目標價(改用期權隱含/skew)—— 蒸餾自 2026-06-19 的三隻研究 agent + SKILL.md。
# 維護時與 resources.py 的 data://report-template 一起改。


@mcp.prompt
def build_stock_report(
    ticker: str, peers: str = "", language: str = "the user's language"
) -> str:
    """Build a polished, self-contained HTML stock report (four lenses + web-news overlay).

    Gathers real numbers via the tools, layers in the latest news via the agent's own web search
    (falling back to `get_company_news`), and renders one self-contained HTML file using the
    `data://report-template` design system. First-party SEC data (Form 4 cluster buying, 13F
    flows, full options greeks) is the hero; honesty rules carried (as-of stamps, missing lens
    dropped from the denominator -> "based on N/4", no invented price target — options-implied
    move/skew instead); EOD/filing-based scope stated up front. Pass `peers` (comma-separated)
    to also emit a head-to-head comparison. Final report renders in the user's language.

    Args:
        ticker: US stock symbol, e.g. AAPL.
        peers: Optional comma-separated peer tickers for a head-to-head comparison, e.g. "MSFT,GOOGL".
        language: Output language for the report (defaults to the user's language).
    """
    peer_line = ""
    if peers.strip():
        peer_line = (
            f"\n\n**Comparison mode.** Peers: {peers}. Run the same tool reads for each peer, then add "
            "a head-to-head **comparison** section: one verdict card per ticker plus a `.cmp` comparison "
            "table (revenue YoY, operating margin, forward P/E, FCF, 3/6-month momentum, 52-week position), "
            "and close with who leads on which axis + the shared risk both face."
        )
    return f"""\
Respond in {language}. Build a polished, **self-contained HTML** stock report for **{ticker}**.

This dataset is **EOD and filing-based — not real-time**: it is built for fundamental, ownership,
and positioning analysis (multi-day to multi-quarter horizon), NOT intraday execution / day
trading. State that scope once near the top, stamp every data block with its **as-of** date, and
lead with a provenance line — data is sourced and normalized directly from **SEC EDGAR** filings +
primary feeds, not scraped. Honesty rules throughout: every number carries an as-of date; missing
data is marked with a white circle and **dropped from the denominator** (say "based on N/4 lenses");
**never invent a price target or forecast** — use the options-implied move / IV / **skew** as the
market-expectation read instead. Use tools for every number — never your memory (it is stale).

**Step 1 — Gather real numbers (call the tools).** Resolve with `search_companies` / `get_company`
(sector sets the interpretation thresholds). Then the four lenses, **first-party panels first**:
- Ownership (the hero): `list_insider_trades` (only `transaction_code` P/S; **cluster buying** =
  >=3 distinct insiders buying within ~90 days, C-suite weighted, NOT A/M/G/F codes) +
  `list_13f_top_buyers` / `list_13f_top_sellers` / `list_13f_holders` (new / exit / big deltas;
  **annotate the ~45-day 13F filing lag** — stamp quarter-end vs filing date).
- Options (build the FULL analytics, not one put/call line): `get_option_expirations` +
  `get_options_chain`. Compute the **ATM IV term structure** (ATM = |delta|≈0.5 call, one point per
  expiry across ALL expiries), pull ONE ~30-45 DTE expiry's full strike ladder for the **IV skew
  curve** and the **open-interest-by-strike** chart, P/C (volume & OI), IV rank, **max pain**
  (argmin over strikes of total option payout), and **expected move** = price × ATM_IV × √(DTE/365).
  Cast JSON-string numbers to float; skip illiquid contracts.
- Fundamentals: `get_overview` + `get_income_statements` / `get_balance_sheets` /
  `get_cash_flow_statements` (quarterly, ~8) — revenue & EPS YoY vs the same quarter last year,
  margin trend, net debt, FCF (`operating_cash_flow - |capex|` when null); **TTM = sum of the 4
  latest quarter rows, never an FY row**; loss-makers drop P/E (label "N/A (loss)").
- Technical: ONE `list_daily_prices` (~1y) -> close vs 50/200-day MA, 52-week position, 1/3/6-month
  momentum, volume vs ~50-day average. `get_overview` `beta` for volatility.
- Events: `list_earnings` (next report date — flag if imminent) + `list_dividends`. See
  `data://dictionary` for field semantics; `get_objective_report` bundles much of this in one call;
  pro tier can aggregate with `execute_readonly_sql`.
- Earnings call (記者會 / 法說會 — **required section**): `get_earnings_transcript` for the latest
  call — management **tone + forward guidance** (filter `speaker` to the CEO/CFO or page with
  `offset`; coverage skews mid/large-cap, so if there is no transcript, say so explicitly).
- Valuation (FULL set, never one or two): from `get_overview` take P/E, forward_pe, peg, P/S, P/B,
  ev_to_ebitda; and **compute** EV = market cap + total debt − cash, EV/Sales = EV / TTM revenue,
  P/FCF + FCF yield. Loss-makers → P/E & EV/EBITDA "N/M", lead P/S + EV/Sales + a Rule-of-40 line.
- Short interest & float: call `get_short_interest` for FINRA open-position history
  (current/prior short shares, short % of float, days-to-cover, settlement date) and `get_overview`
  for shares outstanding / float. Optionally call `get_short_volume` as a separately labelled
  off-exchange flow context; **never substitute daily short-sale volume for open short interest**.
  Cross-check insider/institution ownership against first-party Form 4 / 13F.

**Step 2 — Web news overlay.** If you have **web search** / fetch tools, search: recent news +
the cause of any large price move, latest earnings + management guidance, capital-structure events
(offering / convertible / going-concern), and **verify each DB anomaly** (quarter-end cash runway,
anomalous Form 4, missing earnings date) — each item with source + date. If you have NO web tools,
fall back to the `get_company_news` MCP tool and **state in the report that live web news was
omitted**. Keep web news in its own clearly-labelled section so it never contaminates the cited
first-party core.

**Step 2.5 — Build the thesis (institutional rigor — THIS is the buy-side edge).** A real buy-side
report's edge is **variant perception**: the divergence between what the market is pricing in and what
the data says (a no-consensus dataset is not crippled — surface a differentiated, defensible read of
first-party data vs the price).
- **Market-implied read (reverse-DCF):** back out the growth the current multiple embeds, using
  justified-multiple algebra with the firm's OWN fundamentals + a **disclosed** discount rate r —
  g implied by P/E = Payout×(1+g)/(r−g), or P/B = (ROE−g)/(r−g), or P/S = NetMargin×Payout×(1+g)/(r−g).
  Frame as "at today's multiple the market is pricing in ~X% growth — plausible vs its own 5-yr history
  / sector?" An **implied/justified read, NOT a forecast or price target**; state the r you assumed.
- **Our read:** what the first-party signals say (insider cluster/selling, 13F flow, options IV/skew,
  margin/FCF trajectory). State the **divergence** explicitly — and because being different only pays
  if it is right, a one-line **"what would make this wrong"** (a falsifier).
- **Scenarios (base / bull / bear)** with disclosed inputs, never vibes: base = reverse-DCF implied
  growth; bull/bear = the company's own ~5-yr valuation-multiple percentile band (re-rate / de-rate);
  size the move with the **options-implied move** (ATM straddle ≈ 1σ to the next event,
  price×IV×√(DTE/365)) and read put/call skew as the market's asymmetry. **Always include a downside.**
- **Honesty spine (adopt CFA V(A)/V(B) norms):** separate **fact from opinion** — write "implies /
  we read / we expect", never "will"; quant outputs (reverse-DCF, options-implied) are **estimates,
  not certainties**; disclose the method's limits — **no analyst consensus, EOD/not real-time, 13F
  ~45-day lag, the assumed r**; weight first-party SEC data above web/aggregators.

**Step 3 — Render the HTML.** Read the **`data://report-template`** resource, copy its `<style>` and
its Chart.js setup verbatim, and assemble the body from its component patterns with your real values.
Output ONE **self-contained** HTML document (inline CSS/JS; Chart.js 4.4.7 via CDN is the only
external dep). **Charts are mandatory** (a wall of text is a failure): (1) price + MA50 + MA200 +
volume; (2) IV term structure; (3) IV skew curve; (4) open-interest by strike; (5) ownership trend.
**Each section is a FULL block**, not one line: a complete valuation table, a short-interest & float
block + squeeze checklist, and the options-analytics table (term/skew/OI/max-pain/expected-move).
Order: verdict card (overall traffic light + "based on N/4 lenses", one sentence: strongest bull
reason + biggest risk) -> lens lights -> **Thesis (variant perception: market-implied growth vs our
first-party read + what would make us wrong)** -> KPI dashboard -> price+MA+volume chart -> Insider ->
Institutional (+ ownership-trend chart) -> Options (3 charts + analytics table) -> Financials (trend
table) -> Earnings-call takeaways (mgmt tone + guidance) -> Valuation table (incl. market-implied
growth + each multiple's own 5-yr percentile) -> **Scenarios (base/bull/bear + options-implied move)**
-> Short-interest & float -> Technical -> events -> Recent news (web, separate) -> **How we analyze &
its limits** (method + gaps: no consensus, EOD, 13F lag, assumed r) -> Sources & freshness ->
disclaimer. Numbers in monospace; pair every traffic-light color with a word. Before emitting,
self-check: every claim traces to a number on the page; **fact separated from opinion (no "will")**;
**market-implied growth (reverse-DCF) shown instead of a fabricated target**; a **downside (bear)
scenario** present; the method's limits disclosed; no lens scored without data; the 5 charts present.{peer_line}
"""


# ============================================================
# company_profile —— 介紹型報告(這家公司在幹嘛,讓人快速看懂)
# ============================================================
# 與 build_stock_report(買方深度)成對:profile 偏質性、以「找出並說明業務」為核心,
# 第一手業務描述取自 10-K Item 1 Business(get_filing_section)+ agent 自己的 web search,
# 同樣用 data://report-template 渲染、報告語言隨使用者。買方深度報告用 build_stock_report。


@mcp.prompt
def company_profile(ticker: str, language: str = "the user's language") -> str:
    """Plain-English company PROFILE (what the business is) as a self-contained HTML page.

    For a reader who wants to quickly understand the company — what it does, how it makes money,
    its segments/products, market position, key facts, and a light financial snapshot. This is an
    INTRODUCTION, not a buy/sell analysis (use `build_stock_report` for the deep buy-side report).
    The business narrative is sourced first-hand from the 10-K "Item 1 Business" section
    (`get_filing_section`) plus the agent's own web search; renders with the `data://report-template`
    design system. Final report in the user's language.

    Args:
        ticker: US stock symbol, e.g. AAPL.
        language: Output language for the report (defaults to the user's language).
    """
    return f"""\
Respond in {language}. Build a plain-English **company profile** of **{ticker}** as a self-contained
HTML page — goal: the reader understands the business in ~2 minutes. This is an INTRODUCTION, not a
buy/sell call (for the deep buy-side report, use the `build_stock_report` prompt). Every fact carries
an as-of date; data is EOD/filing-based; not investment advice.

**Step 1 — Identify the business (the core — go find it).**
- `get_company` for name / sector / industry / exchange.
- Pull the **first-hand business description**: `list_filings` (form_type `10-K`, newest; ADRs use
  `20-F`) -> `list_filing_sections` -> `get_filing_section` for **Item 1 "Business"**. Summarize what
  the company actually does, its products / services, and **how it makes money**.
- **Web search** (if available) to confirm + freshen: "<company> business model / segments / main
  products / customers / competitors / recent developments" — each with source + date. If you have no
  web tools, state the profile is filing-only.

**Step 2 — Light facts & financials (context, not deep quant).**
- `get_overview`: market cap + a one-line valuation note (NOT the deep ratio table — that's the
  buy-side report's job).
- `get_income_statements` (annual ~4 or quarterly ~8): revenue trend + a YoY growth headline and
  whether it is profitable. ONE `list_daily_prices` (~1y) for a price + volume trend chart.
- Segment / revenue mix from the 10-K if disclosed (else "not broken out").

**Step 3 — Render the HTML.** Read `data://report-template`, copy its `<style>`, and assemble a
PROFILE layout (not the four-lens one): header (ticker, name, sector, mkt cap, a one-line "what they
do", As-of + EDGAR provenance) -> **What the company does** (business overview, products, how it makes
money) -> **Segments / revenue mix** (a chart or list if disclosed) -> **Market position &
competitors** -> **Key facts** strip (mkt cap, exchange, HQ / employees / founded if found) ->
**Financial snapshot** (revenue trend + the price+volume chart, one-line profitability) -> **Recent
developments** (web, separate section) -> Sources & disclaimer. Keep it concrete and readable; cite
the 10-K and any web source; every number carries an as-of date; not investment advice.
"""
