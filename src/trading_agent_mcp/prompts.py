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
you may cite with attribution).

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
always state "based on N/4 lenses". No forecasts, no price target — describe the present.

Order of the report: Fundamentals (what is this company) -> Ownership (who is buying) ->
Technical (price state) -> Options (market expectation), then events + macro context.

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

**Synthesize.** One-line overall verdict naming the strongest bullish reason + the biggest
risk, the per-lens light row, and "based on N/4 lenses" with any ⚪ lens named.

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
loss-making name drop P/E and compare on P/S + growth; no price targets or forecasts.

Close with **one sentence**: who leads on which axis (growth / profitability / valuation /
FCF / momentum) and the **shared risk** both face.
"""
