# Stockfacts MCP

> **A US-equity dataset normalized and documented for AI agents — served over the Model Context Protocol.**
> SEC filings, financials, Form 4 insider trades, 13F institutional holdings, options
> Greeks, FINRA short data, macro series, and a guarded read-only SQL tool.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](./LICENSE)
[![MCP Registry](https://img.shields.io/badge/MCP-io.github.AAAZZZR%2Fstockfacts-635BFF.svg)](https://registry.modelcontextprotocol.io)
[![Python](https://img.shields.io/badge/python-3.12%2B-3776AB.svg)](https://www.python.org/)

**Hosted service:** `https://mcp.livermore.club/mcp` · **Website:** [Stockfacts](https://livermore.club)

**52 tools · 5 prompts · 3 resources.** Every tool is read-only. Prices and fundamentals are
end-of-day (not real-time). `null` always means *not covered* — never zero.

**[English](#english) · [中文](#中文)**

---

<a name="english"></a>

## What this is

Stockfacts MCP is a [Model Context Protocol](https://modelcontextprotocol.io) server that gives
any MCP client (Claude Desktop, claude.ai, Cursor, or your own agent) a **US-equity dataset that
is normalized and documented specifically for AI agents to reason over** — not a raw API dump.
Messy primary sources (SEC XBRL filings, 13F, Form 4, FINRA, market feeds) are parsed, normalized
to stable shapes, and served as read-only tools an agent can compose into an answer.

### Built for agents, not just for humans

- **Self-describing.** A data dictionary (`data://dictionary`) documents every field's meaning,
  unit and edge case — including the strict rule that `null` means *not covered*, never `0` — so
  an agent interprets numbers correctly instead of guessing.
- **Honest about its own limits.** `get_data_coverage` and `start_here` report exactly what is
  covered and how fresh each domain is, so an agent knows the boundary of what it can claim.
- **Consistent conventions everywhere.** Raw USD (never thousands/millions), `YYYY-MM-DD` dates,
  the same shapes across all 52 tools — no per-endpoint surprises for a model to trip over.
- **First-party, normalized.** XBRL financials normalized across fiscal calendars, institutional
  holdings self-computed from raw 13F, filing text split into addressable sections — authoritative
  regulatory data turned into clean, queryable structure.
- **Tool-shaped, with workflows.** 52 read-only tools + 5 analysis-workflow prompts + 3 resources,
  so the agent composes a sourced answer rather than parsing raw JSON.
- **Anti-hallucination by design.** Tools and prompts instruct the agent to cite sources, separate
  first-party from third-party data, and flag reporting lag (e.g. 13F's ~45-day delay).

**Data is end-of-day, not real-time.** This is a research-and-analysis dataset (~5 years of US
equities), not an execution or trading-signal feed — a deliberate trade-off of depth, correctness
and provenance over latency.

The three surfaces — **52 tools**, **5 prompts**, **3 resources** — are detailed below.

## Data disclosure & sources

| Domain | Source | Nature |
|---|---|---|
| Companies, filings, filing sections | **SEC EDGAR** | First-party regulatory filings |
| Income / balance / cash-flow statements | **SEC EDGAR** (XBRL, normalized) | First-party |
| Insider trades (Form 4) | **SEC EDGAR** | First-party |
| Institutional holdings (13F-HR) | **SEC EDGAR** | First-party, self-computed |
| Ownership breakdown, float | **SEC 13F + Form 4** | First-party, self-computed |
| Daily / hourly prices | First-party feed | EOD daily (~5y), hourly (60-day rolling) |
| Dividends, splits, earnings dates | Third-party API | Vendor |
| Earnings-call transcripts, news + sentiment | Third-party API | Vendor-aggregated |
| ETF profile / holdings / sector weights | Third-party API | Vendor |
| Market movers, IPO calendar | Third-party API | Vendor |
| Options EOD (quotes + IV + greeks) | Vendor EOD | End-of-day only |
| Valuation snapshot (`overview`) | Derived | Computed |
| Macro / commodity / index series | Third-party API | Vendor |
| Short interest, short volume | **FINRA** | Regulatory |

**Coverage & conventions**
- US equities only. No real-time quotes, no analyst estimates beyond what vendors provide, no intraday below the hourly bar.
- All monetary values are **raw USD** (not thousands/millions). Dates are `YYYY-MM-DD`.
- `null` means *not covered / not reported* — it is never a substitute for `0`.
- Ratios such as margins, yields and ownership percentages are **0–1 decimals** unless noted (`change_pct` on movers and `short_percent_float` are 0–100).
- Call **`get_data_coverage`** before quoting any precise figure — it reports the real freshness and gaps per domain.

> **Not investment advice.** This project and its data are provided for informational and
> research purposes only. Verify against primary sources before making decisions. Regulatory
> data (SEC, FINRA) and third-party market-data providers are subject to their respective terms;
> confirm your redistribution rights before offering this data commercially.

## Quick start — hosted

### OAuth sign-in (recommended)

```sh
claude mcp add --transport http stockfacts https://mcp.livermore.club/mcp
```

1. Run the command above.
2. A browser window opens for Google sign-in — approve the authorization. If no browser
   opens, run `/mcp` inside Claude Code and pick this server to trigger it.
3. Done. The access token lasts 1 hour and the refresh token 30 days, and the client renews
   both on its own, so **you never copy or paste a key**.

> **Requires OAuth to be enabled on the server** (admin configuration). If it is not enabled,
> `claude mcp add` returns 401 — use the API key method below instead.

### API key

For scripts, CI, non-interactive environments, and any time the server does not have OAuth
enabled.

1. Create an API key on the [Stockfacts website](https://livermore.club).
2. Point your MCP client at the hosted endpoint with the key as a Bearer token.

**Claude Desktop** (`claude_desktop_config.json`) via [`mcp-remote`](https://www.npmjs.com/package/mcp-remote):

```json
{
  "mcpServers": {
    "stockfacts": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote",
        "https://mcp.livermore.club/mcp",
        "--header", "Authorization: Bearer ${STOCKFACTS_KEY}"
      ],
      "env": { "STOCKFACTS_KEY": "sk_your_key_here" }
    }
  }
}
```

**claude.ai / any client with native remote-MCP support:** add the URL
`https://mcp.livermore.club/mcp` and set the `Authorization: Bearer <key>` header.

Once connected, call **`start_here`** first — it returns the product scope, a tool-selection
guide, and end-to-end workflows.

## Quick start — self-host

```sh
git clone https://github.com/AAAZZZR/Trading_Agent_MCP.git
cd Trading_Agent_MCP
cp .env.example .env          # fill in MCP_API_BASE_URL + tokens (see Configuration)
uv sync

# Streamable HTTP (public transport, default port 8000)
uv run python -m trading_agent_mcp

# or stdio (Claude Desktop spawns locally, no auth)
uv run python -m trading_agent_mcp --stdio
```

The server talks to a Stockfacts REST API (`MCP_API_BASE_URL`). To run the whole stack yourself
you also need that API and its Postgres database — see [Trading_Agent](https://github.com/AAAZZZR/Trading_Agent).

## Authentication

`_build_auth()` picks one mode from settings:

| Mode | Enabled when | Behaviour |
|---|---|---|
| **OAuth (Google)** | `MCP_GOOGLE_CLIENT_ID` + `MCP_GOOGLE_CLIENT_SECRET` + `MCP_PUBLIC_BASE_URL` + `MCP_SAAS_DATABASE_URL` all set | Standard MCP OAuth 2.1 with Google as the upstream identity provider. The client discovers the authorization server from the well-known metadata, registers itself via Dynamic Client Registration (`POST /register`), opens a browser for Google sign-in, and receives access + refresh tokens that it renews on its own. **No API key is ever copied by hand.** The Google identity is mapped to a `users` row (created on first sign-in, or bound to an existing password account with the same verified email); scopes reflect the user's tier exactly as in per-user mode. `idb_`-prefixed API keys still work in this mode — they are routed to the per-user verifier below, so both paths coexist. **CIMD is deliberately disabled** (`enable_cimd=False`): it requires the server to fetch the client's metadata document, and this deployment's egress IP gets a Cloudflare challenge (HTTP 403) on `claude.ai`. Leaving it on would advertise `client_id_metadata_document_supported` in the authorization-server metadata, which makes clients prefer CIMD, skip DCR, and fail at `/authorize` with "Client Not Registered". |
| **Per-user (SaaS)** | `MCP_PER_USER_AUTH=true` **and** `MCP_SAAS_DATABASE_URL` set | Each user sends their own API key, validated directly against the SaaS control-plane database (`MCP_SAAS_DATABASE_URL`), with a short positive cache; metering is fire-and-forget per tool call. Scopes reflect the user's tier. |
| **Shared token** | per-user off + `MCP_BEARER_TOKEN` set | A single shared bearer (scope `tier:pro`, full access). Backwards-compatible. |
| **No auth** | `MCP_PER_USER_AUTH=false` and no shared token | No verifier — for local stdio development. |
| **Misconfigured** | per-user on, but neither `MCP_SAAS_DATABASE_URL` nor `MCP_BEARER_TOKEN` set | Every request is rejected with 401 and a startup error names the missing variable. Deliberately **not** a fall-through to "no auth": a public server must never open up because one env var is missing. |

Modes are tried top to bottom. If the OAuth variables are incomplete the server falls through to the next mode instead of failing to start.

- **User flow with OAuth:** `claude mcp add stockfacts https://.../mcp` → the client follows the 401's `resource_metadata` to `/.well-known/oauth-authorization-server` → a browser opens → sign in with Google → done. Access tokens last 1 hour and refresh tokens 30 days, both renewed by the client without user action; a new machine or a new session just repeats the browser step.
- **Google OAuth app setup (self-host):** create a *Web application* OAuth client and add `<MCP_PUBLIC_BASE_URL>/auth/callback` to its Authorized redirect URIs. No extra signing or encryption secret is needed — the JWT signing key and the OAuth-state encryption key are both derived deterministically from the client secret, so they stay stable across pods and redeploys (rotating the client secret invalidates existing OAuth sessions, which users fix by authorizing once more).
- **OAuth state lives in Postgres** (table `mcp_oauth_state`, created automatically) and is encrypted at rest, because it holds users' upstream Google tokens. The framework default is an on-disk file store, which would be wiped every time a container is replaced.
- Without OAuth configured, the server does **not** advertise OAuth metadata, so a 401 surfaces as a plain auth failure rather than kicking the client into an OAuth flow.
- **Tier gating:** only `execute_readonly_sql` and `describe_table` require `tier:pro`. On a `tier:free` key they don't appear in `tools/list` and are blocked if called directly. The other 50 tools are available to free and pro.
- **Caching:** a verified key is cached in-process for `MCP_AUTH_CACHE_TTL` (5 min) and does not touch the database during that window — the trade-off is that a revoked key stays usable for at most that long. If the database is briefly unreachable, a previously verified key keeps working for up to `MCP_AUTH_STALE_TTL` (60 min); with no cache entry the request is rejected.
- **Quota is not a broken key:** running out of free-tier calls still authenticates — the connection stays up and only `tools/call` is refused, with a message saying the key is still valid.

## Tool reference

52 tools, grouped by the sections in `tools.py`. Naming convention: `list_` = many rows,
`get_` = one row, `search_` = fuzzy lookup, `screen_` = cross-market filter, `describe_` = schema
introspection. Unless noted, a "not found" on a single-object tool raises a tool error (HTTP 404);
list/screen tools return an empty result instead.

### Onboarding

| Tool · Endpoint | What it does | Params (default) | Returns |
|---|---|---|---|
| **`start_here`** · *(no backend call)* | First stop for any new agent: product scope, tool-selection guide, workflows, prompt/resource index, honesty rules. | — | `dict` (static, always free & safe) |

### Meta — data coverage (the trust anchor)

| Tool · Endpoint | What it does | Params (default) | Returns |
|---|---|---|---|
| **`get_data_coverage`** · `GET /api/meta/coverage` | What the dataset covers and how fresh each domain is. Call before quoting exact numbers. | — | `dict{as_of, notes, domains[]}` with `last_data_point`, `last_ingest_ok`, `coverage.tickers`, `date_range`, `cadence`, `known_gaps` |

### Companies

| Tool · Endpoint | What it does | Params (default) | Returns |
|---|---|---|---|
| **`list_companies`** · `GET /api/companies` | Browse/sample tracked companies, alphabetical by ticker. | `limit=200`, `offset=0` | `list[dict]` — ticker, cik, name, sector, sic_code, industry, exchange, is_active, first_seen, last_updated |
| **`search_companies`** · `GET /api/companies/search` | Fuzzy typeahead by ticker or name. | `q`, `limit=20` (1–50) | `list[dict]` — ticker, name, exchange |
| **`get_company`** · `GET /api/companies/{ticker}` | One company's full profile by exact ticker. | `ticker` | `dict` — profile + status ('active'/'delisted'), delisted_at |

### Filings (SEC)

| Tool · Endpoint | What it does | Params (default) | Returns |
|---|---|---|---|
| **`list_filings`** · `GET /api/filings` | A company's SEC filings, newest filed first. | `ticker`, `form_type=None`, `since=None`, `until=None`, `limit=50` (1–500) | `list[dict]` — accession, form_type, filed_at |
| **`get_filing`** · `GET /api/filings/{accession}` | One filing's metadata by accession number. | `accession` | `dict` (filing meta) |
| **`list_filing_sections`** · `GET /api/filings/{accession}/sections` | Parsed table of contents (item_code + title, no body). | `accession` | `list[dict]` (TOC) |
| **`get_filing_section`** · `GET /api/filings/{accession}/sections/{item_code}` | Full body text of one section. `item_code` must come from `list_filing_sections`. | `accession`, `item_code` | `dict` (section text) |

### Financials (three statements)

`period`: `annual` (FY) or `quarterly` (Q1–Q3; Q4 is implied inside FY). All values USD, newest period first.

| Tool · Endpoint | What it does | Params (default) | Returns |
|---|---|---|---|
| **`get_income_statements`** · `GET /api/financials/income` | Revenue, margins, net income, EPS. | `ticker`, `period=None`, `limit=20` (1–200) | `list[dict]` |
| **`get_balance_sheets`** · `GET /api/financials/balance` | Cash, receivables, PPE, assets, liabilities, equity. | `ticker`, `period=None`, `limit=20` | `list[dict]` |
| **`get_cash_flow_statements`** · `GET /api/financials/cashflow` | Operating CF, capex, FCF, dividends, buybacks. | `ticker`, `period=None`, `limit=20` | `list[dict]` |
| **`get_latest_period`** · `GET /api/financials/latest` | Latest period's three statements bundled. | `ticker`, `period=None` | `dict{period_end, income, balance, cash_flow}` |

### Insider trades (Form 4)

| Tool · Endpoint | What it does | Params (default) | Returns |
|---|---|---|---|
| **`list_insider_trades`** · `GET /api/insider` | One company's insider transactions, newest trade first. | `ticker`, `since=None`, `until=None`, `limit=100` (1–1000) | `list[dict]` — transaction_date, insider_name, insider_title, transaction_code (P=buy/S=sell), shares, price_per_share |

### Prices

| Tool · Endpoint | What it does | Params (default) | Returns |
|---|---|---|---|
| **`list_daily_prices`** · `GET /api/prices/daily` | First-party daily OHLC + volume, date-ascending, ~5y. No adj_close. | `ticker`, `start=None`, `end=None`, `limit=2000` (1–10000) | `list[dict]` — ticker, date, open, high, low, close, volume |
| **`get_latest_price`** · `GET /api/prices/daily/latest` | Latest trading day's daily bar. | `ticker` | `dict{ticker,date,ohlc,volume}` |
| **`list_hourly_prices`** · `GET /api/prices/hourly` | Hourly OHLC + volume (timestamptz), 60-day rolling. | `ticker`, `start=None`, `end=None`, `limit=5000` (1–20000) | `list[dict]` |

*When `start`/`end` are both omitted, the daily/hourly tools auto-derive a start so the default returns "the most recent ~N bars".*

### Holdings (first-party, self-computed)

| Tool · Endpoint | What it does | Params (default) | Returns |
|---|---|---|---|
| **`list_institutional_holders`** · `GET /api/holdings/institutions` | Top institutional holders (from first-party 13F). | `ticker` | `list[dict]` — holder, date_reported, shares, value, pct_held, pct_change |
| **`get_holders_breakdown`** · `GET /api/holdings/major` | Insider vs institutional ownership (13F + Form 4 + float). | `ticker` | `dict` — insiders_pct, institutions_pct, institutions_float_pct, institutions_count |

### 13F-HR (first-party institutional holdings)

Full holder lists and quarter-over-quarter change. JSON mode caps at 1000 rows.

| Tool · Endpoint | What it does | Params (default) | Returns |
|---|---|---|---|
| **`list_13f_holders`** · `GET /api/13f/holders` | All institutional holders of a stock, by market value desc. | `ticker`, `quarter_end=None`, `limit=100` (1–1000) | `list[dict]` — filer_cik, filer_name, cusip, quarter_end, shares, market_value, change_in_shares, change_type |
| **`list_13f_portfolio`** · `GET /api/13f/portfolio` | One institution's entire portfolio by CIK. | `cik`, `quarter_end=None`, `limit=100` | `list[dict]` (ticker may be null) |
| **`list_13f_top_buyers`** · `GET /api/13f/top-buyers` | Institutions adding/opening the most this quarter. | `ticker`, `quarter_end=None`, `limit=50` | `list[dict]` (change_in_shares desc) |
| **`list_13f_top_sellers`** · `GET /api/13f/top-sellers` | Institutions trimming/exiting the most this quarter. | `ticker`, `quarter_end=None`, `limit=50` | `list[dict]` (change_in_shares asc) |
| **`search_institutions`** · `GET /api/13f/institutions/search` | Fuzzy filer lookup by name or CIK. | `q`, `limit=20` (1–50) | `list[dict]` — cik, name, first_seen, latest_quarter |
| **`get_institution`** · `GET /api/13f/institutions/{cik}` | One 13F filer's profile by exact CIK. | `cik` | `dict{cik,name,first_seen,latest_quarter}` |

### Corporate actions (third-party API)

| Tool · Endpoint | What it does | Params (default) | Returns |
|---|---|---|---|
| **`list_dividends`** · `GET /api/dividends/{ticker}` | Cash dividend history, newest ex-date first. | `ticker`, `limit=100`, `offset=0` | `list[dict]` — ex_dividend_date, payment_date, amount (per share) |
| **`list_splits`** · `GET /api/splits/{ticker}` | Split history, newest effective date first. | `ticker`, `limit=100`, `offset=0` | `list[dict]` — effective_date, split_factor (2:1→2.0, 1:10→0.1) |

### Earnings calendar (third-party API)

| Tool · Endpoint | What it does | Params (default) | Returns |
|---|---|---|---|
| **`list_earnings`** · `GET /api/earnings/{ticker}` | One company's (upcoming) earnings dates. | `ticker`, `start=None`, `end=None`, `limit=100` | `list[dict]` — report_date, fiscal_date_ending, estimate_eps, report_time |
| **`get_earnings_calendar`** · `GET /api/earnings/calendar` | Cross-market scan of companies reporting in a window. | `start=None`, `end=None`, `limit=500` (1–2000) | `list[dict]` |

### Earnings-call transcripts (third-party API)

| Tool · Endpoint | What it does | Params (default) | Returns |
|---|---|---|---|
| **`get_earnings_transcript`** · `GET /api/companies/{ticker}/transcripts[/{quarter}]` | Paged transcript segments + call meta. Defaults to 40 segments. | `ticker`, `quarter=None` (latest), `offset=0`, `limit=40` (1–500), `speaker=None` | `dict{quarter, segments_total, segments[], available_quarters}` |

### News + sentiment (third-party API — vendor aggregated)

| Tool · Endpoint | What it does | Params (default) | Returns |
|---|---|---|---|
| **`get_company_news`** · `GET /api/companies/{ticker}/news` | A company's news + sentiment, last N days. | `ticker`, `days=7` (1–90), `min_relevance=0.5`, `limit=20` | `list[dict]` — title, url, published_at, overall_sentiment, ticker_sentiment |
| **`get_market_news`** · `GET /api/market/news` | Market-wide news + sentiment. `topic` is case-sensitive exact match. | `topic=None`, `limit=20` | `list[dict]` — title, url, published_at, sentiment, topics[] |

### ETF (third-party API)

| Tool · Endpoint | What it does | Params (default) | Returns |
|---|---|---|---|
| **`get_etf_profile`** · `GET /api/etf/{ticker}/profile` | Fund-level metadata. | `ticker` | `dict` — net_assets, net_expense_ratio, dividend_yield, leveraged, inception_date |
| **`list_etf_holdings`** · `GET /api/etf/{ticker}/holdings` | Holdings + weights, by weight desc. | `ticker`, `limit=100`, `offset=0` | `list[dict]` — holding_symbol, weight |
| **`list_etf_sectors`** · `GET /api/etf/{ticker}/sectors` | GICS sector weights (~11), by weight desc. | `ticker` | `list[dict]` — sector, weight |
| **`list_etfs_holding_ticker`** · `GET /api/etf/holders/{ticker}` | Reverse lookup: which ETFs hold a symbol. | `ticker`, `limit=100`, `offset=0` | `list[dict]` |

### Macro (third-party API)

| Tool · Endpoint | What it does | Params (default) | Returns |
|---|---|---|---|
| **`list_macro_series`** · `GET /api/macro/series` | Self-describing catalog of macro/commodity/index series. Call before fetching values. | `category=None` ('macro'/'commodity'/'index') | `list[dict]` — series_id, name, unit, frequency, category |
| **`get_macro_series`** · `GET /api/macro/series/{series_id}` | Observations for a series. Defaults newest-first; pass `order='asc'` for charts. | `series_id`, `start=None`, `end=None`, `limit=2000`, `order='desc'` | `list[dict]` — date, value |

### Market snapshot (third-party API)

| Tool · Endpoint | What it does | Params (default) | Returns |
|---|---|---|---|
| **`get_market_movers`** · `GET /api/market/movers` | Top-20 gainers / losers / most-active for a day. | `date=None` (latest) | `dict{gainers[], losers[], most_active[]}` — rank, ticker, price, change_amount, change_pct (0–100), volume |
| **`get_ipo_calendar`** · `GET /api/market/ipo-calendar` | Upcoming/recent IPOs, listing date ascending. | `from_date=None`, `to_date=None` (today..+90d) | `list[dict]` — symbol, ipo_date, name, price_range_low/high, exchange |

### Valuation snapshot

| Tool · Endpoint | What it does | Params (default) | Returns |
|---|---|---|---|
| **`get_overview`** · `GET /api/overview/{ticker}` | Raw valuation snapshot: market cap, P/E family, beta, 52-week range, MAs, analyst target. | `ticker` | `dict` (many nullable) — market_cap, float_shares, pe_ratio, forward_pe, peg_ratio, ev_to_ebitda, margins (0–1), beta, week_52_high/low, ma_50/200, analyst_target_price |

### Options (EOD — quotes + IV + greeks)

The tool param is `ticker`; the underlying API query param is `underlying`. Numeric fields are returned as **JSON strings** — cast to float before math. `null` ≠ 0; greeks can be negative.

| Tool · Endpoint | What it does | Params (default) | Returns |
|---|---|---|---|
| **`get_options_chain`** · `GET /api/options/chain` | Option chain for a day (EOD quotes + IV + greeks). | `ticker`, `as_of=None` (latest), `expiration=None`, `option_type=None`, `limit=250` (1–5000) | `list[dict]` — contract_id (OCC), strike, expiration, bid/ask, volume, open_interest, iv, delta/gamma/theta/vega/rho |
| **`get_option_expirations`** · `GET /api/options/expirations` | Available expirations + contract counts. | `ticker`, `as_of=None` | `list[dict]` — expiration, contract_count |
| **`get_option_contract_history`** · `GET /api/options/contract/{contract_id}` | One OCC contract's daily EOD time series. | `contract_id`, `start=None`, `end=None`, `limit=2000` | `list[dict]` |

### Screeners (cross-market)

Both return `{"items": [...], "count": N}`.

| Tool · Endpoint | What it does | Params (default) | Returns |
|---|---|---|---|
| **`screen_insider_buys`** · `GET /api/screener/insider-buys` | "Who's been buying?" — cross-market insider open-market trades. | `transaction_code='P'`, `since_days=90`, `ticker_contains=None`, `insider_title_contains=None`, `market_cap_min/max=None`, `limit=100` | `dict{items[], count}` |
| **`screen_high_short_interest`** · `GET /api/shorts/screener` | Latest FINRA universe by short % of float desc — potential squeeze / crowded shorts. | `min_short_percent_float=10.0`, `min_days_to_cover=0.0`, `limit=50` | `dict{items[], count}` |

### FINRA short-market data

Short **interest** (open positions, twice-monthly) is distinct from short **volume** (daily off-exchange flow). Both list tools return an empty list (not 404) when a ticker has no rows.

| Tool · Endpoint | What it does | Params (default) | Returns |
|---|---|---|---|
| **`get_short_interest`** · `GET /api/shorts/interest/{ticker}` | Twice-monthly open short-interest history, newest settlement first. | `ticker`, `limit=24` (1–120, ≈1y) | `list[dict]` — settlement_date, current_short_position, change_percent, days_to_cover, short_percent_float (0–100, null if no float) |
| **`get_short_volume`** · `GET /api/shorts/volume/{ticker}` | Daily off-exchange short-sale volume (flow, not open interest). | `ticker`, `days=30` (1–365), `limit=200` | `list[dict]` — trade_date, short_volume, short_exempt_volume, total_volume, short_volume_percent |

### Analysis & reports (interpretation)

| Tool · Endpoint | What it does | Params (default) | Returns |
|---|---|---|---|
| **`get_analysis`** · `GET /api/analysis/{ticker}` | Four-lens traffic-light analysis (fundamentals/ownership/technical/options + overall). Always 200; missing lens → `verdict="na"`. | `ticker` | `dict` — overall_verdict, lenses[4]{verdict, summary, signals[]} |
| **`get_objective_report`** · `GET /api/report/{ticker}` | One-shot objective data pack (valuation + statements + insider + 13F + price/options summaries + filing titles). Raw data, no scoring. | `ticker`, `sections=None`, plus per-block limits | `dict` — company + 8 `{source, as_of, …}` blocks |

### Read-only SQL (⭐ pro tier)

Direct read-only Postgres — the only tools that don't call the REST API. Require `tier:pro`. See [Read-only SQL safety](#read-only-sql-safety) below.

| Tool · Backing | What it does | Params (default) | Returns |
|---|---|---|---|
| **`execute_readonly_sql`** · readonly PG pool | Run one read-only `SELECT` for flexible/statistical queries. | `query` (single SELECT) | JSON string `{row_count, executed_query, rows[]}` |
| **`describe_table`** · `information_schema` | Introspect the SQL schema before writing a query. | `table_name=None` (omit → list tables) | JSON string `{tables[]}` or `{table, columns[]}` |

## Prompts

Reusable workflows (`prompts.py`). Each instructs the agent to reply in the user's language.

| Prompt | Purpose |
|---|---|
| **`analyze_stock(ticker)`** | Simple price + financials analysis as a conclusion-first tool workflow. |
| **`analyze_stock_full(ticker)`** | Full four-lens (fundamentals / ownership / technical / options) traffic-light report, with honest "based on N/4" handling and 13F-lag notes. |
| **`compare_stocks(ticker_a, ticker_b)`** | Side-by-side comparison on growth, margins, valuation, FCF, momentum. |
| **`build_stock_report(ticker, peers="", language=...)`** | Build a polished, self-contained HTML stock report (four lenses + web-news overlay + reverse-DCF variant perception), rendered with `data://report-template`. |
| **`company_profile(ticker, language=...)`** | Plain-English "what this company does" HTML profile (from 10-K Item 1), not a buy/sell call. |

## Resources

| Resource | Contents |
|---|---|
| **`data://dictionary`** | Field-level semantics, units, transaction codes, filing lag, `null ≠ 0` rules, and which tool serves each domain. Read before interpreting numbers. |
| **`data://analysis-playbook`** | Document form of the `analyze_stock` methodology, for clients without MCP-prompt support. |
| **`data://report-template`** | Self-contained HTML design system + assembly guide for `build_stock_report`. |

## Read-only SQL safety

`execute_readonly_sql` accepts arbitrary `SELECT` / `WITH … SELECT`, protected by three layers:

1. **SQL parsing** (`sqlparse`): single statement only; the first meaningful keyword must be `SELECT`/`WITH`; rejects `INSERT/UPDATE/DELETE/DROP/CREATE/ALTER/TRUNCATE/GRANT` and sensitive functions (`pg_sleep`, `pg_read_server_files`, `lo_import`, `dblink`, `COPY`, …); multi-statement is rejected.
2. **DB role**: the connection uses the `investor_db_readonly` role, granted `SELECT` only — the true last line of defence.
3. **Statement timeout**: 5 s per query; complex queries abort.

- **LIMIT injection**: the query is wrapped as `SELECT * FROM (<your sql>) _ LIMIT n`. No top-level `LIMIT` → `1000`; explicit `LIMIT N` → `min(N, 10000)`. Only a literal integer `LIMIT` counts; a `LIMIT` inside a subquery cannot bypass the outer bound.
- **Output truncation**: payloads over 100 KB are truncated with a note.

### Read-only DB role setup (self-host)

```sh
# 1. Edit scripts/grant_readonly.sql, replace CHANGE_ME with a password.
# 2. Run as a superuser against your DB:
psql "<superuser DSN>" -f scripts/grant_readonly.sql
# 3. Set the DSN (asyncpg style, no +asyncpg suffix):
#    MCP_READONLY_DB_DSN=postgresql://investor_db_readonly:<pw>@<host>:<port>/<db>
```

If `MCP_READONLY_DB_DSN` is empty the two SQL tools are disabled (they return a friendly error); every other tool is unaffected.

## Configuration

| Env var | Required | Default | Purpose |
|---|---|---|---|
| `MCP_API_BASE_URL` | ✅ | — | Stockfacts REST API base URL (no trailing slash). |
| `MCP_API_AUTH_TOKEN` | ✅ | — | Service bearer: MCP server → REST API. |
| `MCP_PUBLIC_BASE_URL` | — | `""` | Publicly reachable base URL, no trailing slash. The OAuth issuer, metadata URLs and the Google redirect URI (`<this>/auth/callback`) are all derived from it. Empty disables OAuth. |
| `MCP_GOOGLE_CLIENT_ID` | — | `""` | Google OAuth 2.0 *Web application* client ID. |
| `MCP_GOOGLE_CLIENT_SECRET` | — | `""` | Google OAuth client secret. Also the source material for the JWT signing key and the OAuth-state encryption key, so no separate secrets are needed. |
| `MCP_BEARER_TOKEN` | — | `""` | Shared client bearer (shared-token mode only). |
| `MCP_PER_USER_AUTH` | — | `true` | `true` = per-user SaaS auth; `false` = shared token. |
| `MCP_SAAS_DATABASE_URL` | — | `""` | SaaS control-plane Postgres DSN (`api_keys` / `subscriptions` / `usage_events` / `users`, plus the auto-created `mcp_oauth_state`). Needs read **and** write, so it is not the read-only role below. Empty disables both OAuth and per-user auth. |
| `MCP_AUTH_CACHE_TTL` | — | `300` | Positive auth cache TTL (seconds); a revoked key stays usable for at most this long. |
| `MCP_AUTH_STALE_TTL` | — | `3600` | How long a cached key keeps working while the database is unreachable (seconds). |
| `MCP_QUOTA_CACHE_TTL` | — | `60` | Quota-state cache TTL (seconds). |
| `MCP_READONLY_DB_DSN` | — | `""` | Read-only Postgres DSN for the SQL tools. Empty disables them. |
| `PORT` | — | `8000` | Streamable HTTP port. |

## Deployment

Any container platform works — all platform dependencies are env-injected. A root `Dockerfile`
is auto-detected. Expose the HTTP port and set the env vars above; the streamable-HTTP endpoint
is served at `/mcp`.

## Development

```sh
uv sync
uv run pytest -q        # 857 tests
```

## License

[Apache-2.0](./LICENSE) © 2026 AAAZZZR — Stockfacts.

---

<a name="中文"></a>

## 這是什麼

Stockfacts MCP 是一個 [Model Context Protocol](https://modelcontextprotocol.io) server,給任何
MCP client(Claude Desktop、claude.ai、Cursor 或你自己的 agent)一套**專門為 AI agent 正規化、
並附完整文檔的美股資料集**——不是把原始 API 直接丟出來。零散的第一手來源(SEC XBRL 申報、13F、
Form 4、FINRA、市場行情)先被解析、正規化成穩定結構,再以唯讀 tool 的形式提供,讓 agent 直接組
裝成答案。

### 為 AI agent 而做,不只是給人看

- **自我描述。** 一份資料字典(`data://dictionary`)寫清楚每個欄位的語意、單位與陷阱——包含
  嚴格規則「`null` 代表未涵蓋、絕不等於 `0`」——讓 agent 正確解讀數字,而不是用猜的。
- **對自己的極限誠實。** `get_data_coverage` 與 `start_here` 明確回報「涵蓋了什麼、各 domain 多
  新」,agent 因此知道自己能宣稱的邊界在哪。
- **全域一致的慣例。** 一律 raw USD(不用千/百萬)、`YYYY-MM-DD` 日期、52 個 tool 同樣的回傳
  形狀——沒有各接口各一套的陷阱來絆倒模型。
- **第一手、已正規化。** XBRL 財報跨會計年度正規化、機構持股由原始 13F 自算、filing 內文切成可
  定址的章節——把權威法規資料變成乾淨可查的結構。
- **工具化,附工作流。** 52 個唯讀 tool + 5 個分析工作流 prompt + 3 個 resource,agent 直接組
  出「有出處」的答案,而不是自己去 parse 原始 JSON。
- **設計上防幻覺。** tool 與 prompt 都要求 agent 標註來源、區分第一手與第三方資料、並註明申報
  延遲(如 13F 約 45 天)。

**資料是收盤級(EOD)、非即時。** 這是一套研究與分析用的資料集(約 5 年美股),不是下單或交易
訊號來源——這是刻意的取捨:用深度、正確性與可溯源性換取即時性。

三個表面——**52 tools、5 prompts、3 resources**——詳見下方。

## 資料披露與來源

| 主題 | 來源 | 性質 |
|---|---|---|
| 公司、filings、filing 章節 | **SEC EDGAR** | 第一手法規申報 |
| 損益 / 資產負債 / 現金流量表 | **SEC EDGAR**(XBRL 正規化) | 第一手 |
| 內部人交易(Form 4) | **SEC EDGAR** | 第一手 |
| 機構持股(13F-HR) | **SEC EDGAR** | 第一手,自算 |
| 持股結構、流通股數 | **SEC 13F + Form 4** | 第一手,自算 |
| 每日 / 每小時股價 | 第一手來源 | EOD 日 K(~5 年)、小時 K(60 天滾動) |
| 股利、分割、財報日 | Third-party API | 資料商 |
| 法說會逐字稿、新聞 + 情緒 | Third-party API | 資料商聚合 |
| ETF 概況 / 成分 / sector 權重 | Third-party API | 資料商 |
| 漲跌榜、IPO 行事曆 | Third-party API | 資料商 |
| 期權 EOD(報價 + IV + greeks) | 資料商 EOD | 僅收盤 |
| 估值快照(`overview`) | 衍生計算 | 計算 |
| 總經 / 商品 / 指數 序列 | 第三方 API | 資料商 |
| 空單餘額、空單成交量 | **FINRA** | 法規 |

**涵蓋範圍與慣例**
- 只有美股。無即時報價、無超出資料商範圍的分析師預估、小時 K 以下無盤中資料。
- 金額一律 **raw USD**(非千/百萬)。日期為 `YYYY-MM-DD`。
- `null` 代表**未涵蓋 / 未申報**——絕不等於 `0`。
- 利潤率、殖利率、持股比例等比率除特別說明外皆為 **0–1 小數**(漲跌榜的 `change_pct` 與 `short_percent_float` 是 0–100)。
- 引用任何精確數字前先呼叫 **`get_data_coverage`**——它回報各 domain 的真實新鮮度與已知缺口。

> **非投資建議。** 本專案與其資料僅供資訊與研究用途。做決策前請對照第一手來源查核。法規資料
> (SEC、FINRA)與第三方資料商的資料受各自條款約束;商業對外散布前請先確認你的
> 再散布權利。

## 快速開始 —— 使用托管服務

### OAuth 登入(建議)

```sh
claude mcp add --transport http stockfacts https://mcp.livermore.club/mcp
```

1. 執行上面這行指令。
2. 瀏覽器會自動跳出 Google 登入 —— 同意授權即可。沒跳出的話,在 Claude Code 裡打 `/mcp`
   選這個 server 觸發。
3. 完成。access token 效期 1 小時、refresh token 30 天,client 會自動續期,**全程不需要
   複製貼上任何金鑰**。

> **需 server 端已啟用 OAuth**(管理員設定)。未啟用時 `claude mcp add` 會回 401,請改用
> 下方的 API key 方式。

### API key

用於腳本、CI、非互動環境,或 server 尚未啟用 OAuth 時。

1. 在 [Stockfacts 網站](https://livermore.club)建立一把 API key。
2. 把你的 MCP client 指向托管接口,key 當 Bearer token 帶上。

**Claude Desktop**(`claude_desktop_config.json`,經 [`mcp-remote`](https://www.npmjs.com/package/mcp-remote)):

```json
{
  "mcpServers": {
    "stockfacts": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote",
        "https://mcp.livermore.club/mcp",
        "--header", "Authorization: Bearer ${STOCKFACTS_KEY}"
      ],
      "env": { "STOCKFACTS_KEY": "sk_你的_key" }
    }
  }
}
```

**claude.ai / 任何原生支援 remote MCP 的 client:** 加入 URL
`https://mcp.livermore.club/mcp`,設定 `Authorization: Bearer <key>` header。

連上後先呼叫 **`start_here`**——它回傳產品範圍、選工具指南與端到端工作流。

## 快速開始 —— 自建

```sh
git clone https://github.com/AAAZZZR/Trading_Agent_MCP.git
cd Trading_Agent_MCP
cp .env.example .env          # 填 MCP_API_BASE_URL + tokens(見「設定」)
uv sync

# Streamable HTTP(對外傳輸,預設 port 8000)
uv run python -m trading_agent_mcp

# 或 stdio(Claude Desktop 本機 spawn,無 auth)
uv run python -m trading_agent_mcp --stdio
```

Server 會打一個 Stockfacts REST API(`MCP_API_BASE_URL`)。要完整自建整條 stack,還需要那個
API 與它的 Postgres——見 [Trading_Agent](https://github.com/AAAZZZR/Trading_Agent)。

## 認證

`_build_auth()` 依設定擇一模式:

| 模式 | 啟用條件 | 行為 |
|---|---|---|
| **OAuth(Google)** | `MCP_GOOGLE_CLIENT_ID` + `MCP_GOOGLE_CLIENT_SECRET` + `MCP_PUBLIC_BASE_URL` + `MCP_SAAS_DATABASE_URL` 四者齊全 | 標準 MCP OAuth 2.1,上游身分供應者是 Google。client 從 well-known metadata 自己發現授權伺服器、用 DCR 自己註冊(`POST /register`)、彈瀏覽器讓使用者用 Google 登入,拿到 access + refresh token 後自動續期。**全程不必手動複製貼上任何 API key。** Google 身分會對應到 `users` 一列(第一次登入時建立;若已有同一個已驗證 email 的密碼帳號則綁定它),scope 一樣反映 tier。此模式下 `idb_` 開頭的 API key 仍然可用——會被轉交給下面的 per-user verifier,兩條路並存。**CIMD 刻意關閉**(`enable_cimd=False`):它要求伺服器反過來去抓 client 託管的 metadata 文件,而這個部署的出口 IP 打 `claude.ai` 會吃到 Cloudflare 挑戰(HTTP 403)。開著等於在 authorization-server metadata 公告 `client_id_metadata_document_supported`,client 會因此優先走 CIMD、跳過 DCR,然後卡在 `/authorize` 的「Client Not Registered」。 |
| **Per-user(SaaS)** | `MCP_PER_USER_AUTH=true` **且** `MCP_SAAS_DATABASE_URL` 有值 | 每個 user 帶自己的 API key,直接對 SaaS 控制面 DB(`MCP_SAAS_DATABASE_URL`)驗證,帶短時間的正向快取;計量改成每次 tool 呼叫非同步寫入。scope 反映 user 的 tier。 |
| **共用 token** | per-user 關 + `MCP_BEARER_TOKEN` 非空 | 單一共用 bearer(scope `tier:pro`,完整權限)。向後相容。 |
| **無 auth** | `MCP_PER_USER_AUTH=false` 且無共用 token | 不啟用 verifier——本機 stdio 開發用。 |
| **設定失誤** | per-user 開著,但 `MCP_SAAS_DATABASE_URL` 與 `MCP_BEARER_TOKEN` 都沒設 | 一律回 401,啟動時會 log 出缺哪個變數。刻意**不**掉回「不啟用 auth」——對外的 server 絕不能因為少設一個環境變數就敞開。 |

模式由上往下擇一。OAuth 的變數沒設齊時會落到下一個模式,而不是讓 server 起不來。

- **OAuth 的使用者流程:** `claude mcp add stockfacts https://.../mcp` → client 依 401 的 `resource_metadata` 找到 `/.well-known/oauth-authorization-server` → 彈出瀏覽器 → 用 Google 登入 → 完成。access token 效期 1 小時、refresh token 30 天,都由 client 自動續,使用者不必再操作;換一台電腦或換一個 session 只要再走一次瀏覽器那步。
- **自建時的 Google OAuth app 設定:** 建一個 *Web application* 類型的 OAuth client,並把 `<MCP_PUBLIC_BASE_URL>/auth/callback` 加進 Authorized redirect URIs。不需要額外的簽章 / 加密金鑰變數——FastMCP JWT 的 signing key 與 OAuth 狀態儲存的加密金鑰都從 client secret 決定性推導,跨 pod、跨 redeploy 自動一致(輪替 client secret 會讓既有 OAuth session 失效,使用者重新授權一次即可)。
- **OAuth 狀態存在 Postgres**(表 `mcp_oauth_state`,自動建立)並加密,因為裡面有使用者的上游 Google token。框架預設是本機檔案儲存,容器一換就整包蒸發。
- 沒設定 OAuth 時,server **不**公告 OAuth metadata,401 會直接顯示認證失敗,不會誤把 client 帶進 OAuth 流程。
- **Tier gating:** 只有 `execute_readonly_sql` 與 `describe_table` 需要 `tier:pro`。`tier:free` 的 key 在 `tools/list` 看不到這兩個、直接呼叫也被擋。其餘 50 個 free/pro 皆可用。
- **快取:** 驗過的 key 會在 process 內快取 `MCP_AUTH_CACHE_TTL`(5 分鐘),期間完全不碰 DB——取捨是撤銷最多延遲這麼久才生效。DB 短暫不可用時,之前驗過的 key 還能續用到 `MCP_AUTH_STALE_TTL`(60 分鐘);沒快取則直接拒絕。
- **超額不等於壞 key:** 免費額度用完仍然認證成功——連線不斷,只有 `tools/call` 被擋下,並告知使用者 key 還是有效的。

## 工具參考

52 個 tool,依 `tools.py` 的實際分組。命名慣例:`list_` = 多筆、`get_` = 單筆、`search_` = 模糊
查詢、`screen_` = 跨市場篩選、`describe_` = schema 自省。除特別說明外,單物件 tool 查無會拋 tool
error(HTTP 404);list/screen 類則回空結果。

### 入口 Onboarding

| 工具 · 接口 | 用途 | 參數(預設) | 回傳 |
|---|---|---|---|
| **`start_here`** · *(不打後端)* | 任何新 agent 的第一站:產品範圍、選工具指南、工作流、prompt/resource 索引、誠實引用規則。 | — | `dict`(靜態,永遠免費且安全) |

### Meta —— 資料涵蓋(信任基石)

| 工具 · 接口 | 用途 | 參數(預設) | 回傳 |
|---|---|---|---|
| **`get_data_coverage`** · `GET /api/meta/coverage` | 資料集實際涵蓋範圍與各 domain 新鮮度。引用精確數字前先呼叫。 | — | `dict{as_of, notes, domains[]}`,含 `last_data_point`、`last_ingest_ok`、`coverage.tickers`、`date_range`、`cadence`、`known_gaps` |

### 公司 Companies

| 工具 · 接口 | 用途 | 參數(預設) | 回傳 |
|---|---|---|---|
| **`list_companies`** · `GET /api/companies` | 瀏覽/抽樣被追蹤公司,依 ticker 字母序。 | `limit=200`, `offset=0` | `list[dict]` — ticker, cik, name, sector, industry, exchange, is_active |
| **`search_companies`** · `GET /api/companies/search` | 以 ticker 或名稱模糊 typeahead。 | `q`, `limit=20`(1–50) | `list[dict]` — ticker, name, exchange |
| **`get_company`** · `GET /api/companies/{ticker}` | 以精確 ticker 取單一公司完整基本資料。 | `ticker` | `dict` — 基本資料 + status('active'/'delisted') |

### SEC 申報 Filings

| 工具 · 接口 | 用途 | 參數(預設) | 回傳 |
|---|---|---|---|
| **`list_filings`** · `GET /api/filings` | 某公司 SEC filings,依 filed_at 由新到舊。 | `ticker`, `form_type=None`, `since=None`, `until=None`, `limit=50`(1–500) | `list[dict]` — accession, form_type, filed_at |
| **`get_filing`** · `GET /api/filings/{accession}` | 以 accession number 取單一 filing 後設資料。 | `accession` | `dict`(filing meta) |
| **`list_filing_sections`** · `GET /api/filings/{accession}/sections` | 已解析章節目錄(item_code + 標題,不含內文)。 | `accession` | `list[dict]`(目錄) |
| **`get_filing_section`** · `GET /api/filings/{accession}/sections/{item_code}` | 單一章節完整內文。`item_code` 須先用 `list_filing_sections` 取得。 | `accession`, `item_code` | `dict`(章節全文) |

### 財報 Financials(三大表)

`period`:`annual`(FY)或 `quarterly`(Q1–Q3;Q4 隱含在 FY)。金額 USD,由新到舊。

| 工具 · 接口 | 用途 | 參數(預設) | 回傳 |
|---|---|---|---|
| **`get_income_statements`** · `GET /api/financials/income` | 營收、毛利、營業利益、淨利、EPS。 | `ticker`, `period=None`, `limit=20`(1–200) | `list[dict]` |
| **`get_balance_sheets`** · `GET /api/financials/balance` | 現金、應收、存貨、PPE、總資產、總負債、權益。 | `ticker`, `period=None`, `limit=20` | `list[dict]` |
| **`get_cash_flow_statements`** · `GET /api/financials/cashflow` | 營業現金流、capex、FCF、股利、回購。 | `ticker`, `period=None`, `limit=20` | `list[dict]` |
| **`get_latest_period`** · `GET /api/financials/latest` | 最近一期三表合體。 | `ticker`, `period=None` | `dict{period_end, income, balance, cash_flow}` |

### 內部人交易 Insider(Form 4)

| 工具 · 接口 | 用途 | 參數(預設) | 回傳 |
|---|---|---|---|
| **`list_insider_trades`** · `GET /api/insider` | 單一公司內部人交易,依交易日由新到舊。 | `ticker`, `since=None`, `until=None`, `limit=100`(1–1000) | `list[dict]` — transaction_date, insider_name, insider_title, transaction_code(P=買/S=賣), shares, price_per_share |

### 股價 Prices

| 工具 · 接口 | 用途 | 參數(預設) | 回傳 |
|---|---|---|---|
| **`list_daily_prices`** · `GET /api/prices/daily` | 第一手日 K(OHLC+量),日期升冪,~5 年。無還原價。 | `ticker`, `start=None`, `end=None`, `limit=2000`(1–10000) | `list[dict]` — ticker, date, open, high, low, close, volume |
| **`get_latest_price`** · `GET /api/prices/daily/latest` | 最新交易日日 K。 | `ticker` | `dict{ticker,date,ohlc,volume}` |
| **`list_hourly_prices`** · `GET /api/prices/hourly` | 小時 K(timestamptz),60 天滾動保留。 | `ticker`, `start=None`, `end=None`, `limit=5000`(1–20000) | `list[dict]` |

*`start`/`end` 都省略時,日/小時 K 會自動回推起始,讓預設回「最近約 N 筆」。*

### 持股 Holdings(第一手自算)

| 工具 · 接口 | 用途 | 參數(預設) | 回傳 |
|---|---|---|---|
| **`list_institutional_holders`** · `GET /api/holdings/institutions` | 主要機構持股(第一手 13F 自算)。 | `ticker` | `list[dict]` — holder, date_reported, shares, value, pct_held, pct_change |
| **`get_holders_breakdown`** · `GET /api/holdings/major` | 內部人/機構持股比例總覽(13F + Form 4 + 流通股數)。 | `ticker` | `dict` — insiders_pct, institutions_pct, institutions_float_pct, institutions_count |

### 13F-HR(第一手機構持股)

完整持有人名單與季度變動。JSON 模式硬上界 1000 筆。

| 工具 · 接口 | 用途 | 參數(預設) | 回傳 |
|---|---|---|---|
| **`list_13f_holders`** · `GET /api/13f/holders` | 某股票所有機構持有人,依市值由大到小。 | `ticker`, `quarter_end=None`, `limit=100`(1–1000) | `list[dict]` — filer_cik, filer_name, cusip, quarter_end, shares, market_value, change_in_shares, change_type |
| **`list_13f_portfolio`** · `GET /api/13f/portfolio` | 某機構(CIK)整個持倉組合。 | `cik`, `quarter_end=None`, `limit=100` | `list[dict]`(ticker 可能 null) |
| **`list_13f_top_buyers`** · `GET /api/13f/top-buyers` | 某股票本季增持/新進最多的機構。 | `ticker`, `quarter_end=None`, `limit=50` | `list[dict]`(change_in_shares 遞減) |
| **`list_13f_top_sellers`** · `GET /api/13f/top-sellers` | 某股票本季減持/出清最多的機構。 | `ticker`, `quarter_end=None`, `limit=50` | `list[dict]`(change_in_shares 遞增) |
| **`search_institutions`** · `GET /api/13f/institutions/search` | 以名稱或 CIK 模糊搜 13F filer。 | `q`, `limit=20`(1–50) | `list[dict]` — cik, name, first_seen, latest_quarter |
| **`get_institution`** · `GET /api/13f/institutions/{cik}` | 以精確 CIK 取單一 filer 基本資料。 | `cik` | `dict{cik,name,first_seen,latest_quarter}` |

### 公司行動 Corporate actions(third-party API)

| 工具 · 接口 | 用途 | 參數(預設) | 回傳 |
|---|---|---|---|
| **`list_dividends`** · `GET /api/dividends/{ticker}` | 現金股息歷史,依除息日由新到舊。 | `ticker`, `limit=100`, `offset=0` | `list[dict]` — ex_dividend_date, payment_date, amount(每股) |
| **`list_splits`** · `GET /api/splits/{ticker}` | 股票分割歷史,依生效日由新到舊。 | `ticker`, `limit=100`, `offset=0` | `list[dict]` — effective_date, split_factor(2:1→2.0,1:10→0.1) |

### 財報行事曆 Earnings(third-party API)

| 工具 · 接口 | 用途 | 參數(預設) | 回傳 |
|---|---|---|---|
| **`list_earnings`** · `GET /api/earnings/{ticker}` | 單一公司(即將)財報日。 | `ticker`, `start=None`, `end=None`, `limit=100` | `list[dict]` — report_date, fiscal_date_ending, estimate_eps, report_time |
| **`get_earnings_calendar`** · `GET /api/earnings/calendar` | 掃全市場某區間內公布財報的公司。 | `start=None`, `end=None`, `limit=500`(1–2000) | `list[dict]` |

### 法說會逐字稿 Transcripts(third-party API)

| 工具 · 接口 | 用途 | 參數(預設) | 回傳 |
|---|---|---|---|
| **`get_earnings_transcript`** · `GET /api/companies/{ticker}/transcripts[/{quarter}]` | 分頁逐字稿段落 + 場次 meta。預設 40 段。 | `ticker`, `quarter=None`(最新), `offset=0`, `limit=40`(1–500), `speaker=None` | `dict{quarter, segments_total, segments[], available_quarters}` |

### 新聞 + 情緒 News(第三方 API —— 資料商聚合)

| 工具 · 接口 | 用途 | 參數(預設) | 回傳 |
|---|---|---|---|
| **`get_company_news`** · `GET /api/companies/{ticker}/news` | 某公司近 N 天新聞 + 情緒。 | `ticker`, `days=7`(1–90), `min_relevance=0.5`, `limit=20` | `list[dict]` — title, url, published_at, overall_sentiment, ticker_sentiment |
| **`get_market_news`** · `GET /api/market/news` | 全市場新聞 + 情緒。`topic` 大小寫敏感精確比對。 | `topic=None`, `limit=20` | `list[dict]` — title, url, published_at, sentiment, topics[] |

### ETF(third-party API)

| 工具 · 接口 | 用途 | 參數(預設) | 回傳 |
|---|---|---|---|
| **`get_etf_profile`** · `GET /api/etf/{ticker}/profile` | ETF 層級 metadata。 | `ticker` | `dict` — net_assets, net_expense_ratio, dividend_yield, leveraged, inception_date |
| **`list_etf_holdings`** · `GET /api/etf/{ticker}/holdings` | 成分股 + 權重,依權重由大到小。 | `ticker`, `limit=100`, `offset=0` | `list[dict]` — holding_symbol, weight |
| **`list_etf_sectors`** · `GET /api/etf/{ticker}/sectors` | GICS sector 權重(~11 個),依權重由大到小。 | `ticker` | `list[dict]` — sector, weight |
| **`list_etfs_holding_ticker`** · `GET /api/etf/holders/{ticker}` | 反查有哪些 ETF 持有某 symbol。 | `ticker`, `limit=100`, `offset=0` | `list[dict]` |

### 總經 Macro(第三方 API)

| 工具 · 接口 | 用途 | 參數(預設) | 回傳 |
|---|---|---|---|
| **`list_macro_series`** · `GET /api/macro/series` | 可查 series 的自我描述目錄。取值前先查目錄。 | `category=None`('macro'/'commodity'/'index') | `list[dict]` — series_id, name, unit, frequency, category |
| **`get_macro_series`** · `GET /api/macro/series/{series_id}` | 某 series 觀測值。預設最新在前;畫圖用 `order='asc'`。 | `series_id`, `start=None`, `end=None`, `limit=2000`, `order='desc'` | `list[dict]` — date, value |

### 市場快照 Market(third-party API)

| 工具 · 接口 | 用途 | 參數(預設) | 回傳 |
|---|---|---|---|
| **`get_market_movers`** · `GET /api/market/movers` | 某交易日漲/跌/成交熱榜(各 top 20)。 | `date=None`(最新) | `dict{gainers[], losers[], most_active[]}` — rank, ticker, price, change_amount, change_pct(0–100), volume |
| **`get_ipo_calendar`** · `GET /api/market/ipo-calendar` | IPO 行事曆,依掛牌日由舊到新。 | `from_date=None`, `to_date=None`(今天..+90d) | `list[dict]` — symbol, ipo_date, name, price_range_low/high, exchange |

### 估值快照 Valuation

| 工具 · 接口 | 用途 | 參數(預設) | 回傳 |
|---|---|---|---|
| **`get_overview`** · `GET /api/overview/{ticker}` | 估值快照原始數據:市值、本益比家族、Beta、52 週高低、均線、分析師目標價。 | `ticker` | `dict`(大量 nullable) — market_cap, float_shares, pe_ratio, forward_pe, peg_ratio, ev_to_ebitda, 各利潤率(0–1), beta, week_52_high/low, ma_50/200, analyst_target_price |

### 期權 Options(EOD —— 報價 + IV + greeks)

tool 參數叫 `ticker`,底層 API query 參數是 `underlying`。**數值欄以 JSON 字串回傳**,做數學前轉 float。`null` ≠ 0;greeks 可為負。

| 工具 · 接口 | 用途 | 參數(預設) | 回傳 |
|---|---|---|---|
| **`get_options_chain`** · `GET /api/options/chain` | 某交易日期權鏈(EOD 報價 + IV + greeks)。 | `ticker`, `as_of=None`(最新), `expiration=None`, `option_type=None`, `limit=250`(1–5000) | `list[dict]` — contract_id(OCC), strike, expiration, bid/ask, volume, open_interest, iv, delta/gamma/theta/vega/rho |
| **`get_option_expirations`** · `GET /api/options/expirations` | 可選到期日 + 各到期合約數。 | `ticker`, `as_of=None` | `list[dict]` — expiration, contract_count |
| **`get_option_contract_history`** · `GET /api/options/contract/{contract_id}` | 單一 OCC 合約逐日 EOD 時間序列。 | `contract_id`, `start=None`, `end=None`, `limit=2000` | `list[dict]` |

### 篩選器 Screeners(跨市場)

兩者都回 `{"items": [...], "count": N}`。

| 工具 · 接口 | 用途 | 參數(預設) | 回傳 |
|---|---|---|---|
| **`screen_insider_buys`** · `GET /api/screener/insider-buys` | 「最近誰在買?」跨市場內部人 open-market 交易。 | `transaction_code='P'`, `since_days=90`, `ticker_contains=None`, `insider_title_contains=None`, `market_cap_min/max=None`, `limit=100` | `dict{items[], count}` |
| **`screen_high_short_interest`** · `GET /api/shorts/screener` | 依 short % float 由高到低——找潛在 squeeze / crowded short。 | `min_short_percent_float=10.0`, `min_days_to_cover=0.0`, `limit=50` | `dict{items[], count}` |

### FINRA 空頭資料

空單**餘額**(未平倉,每月兩次)與空單**成交量**(每日場外 flow)是兩回事。查無時 list 類回空 list(非 404)。

| 工具 · 接口 | 用途 | 參數(預設) | 回傳 |
|---|---|---|---|
| **`get_short_interest`** · `GET /api/shorts/interest/{ticker}` | 每月兩次的未平倉空單歷史,最新 settlement 在前。 | `ticker`, `limit=24`(1–120,≈1 年) | `list[dict]` — settlement_date, current_short_position, change_percent, days_to_cover, short_percent_float(0–100,無 float 分母時 null) |
| **`get_short_volume`** · `GET /api/shorts/volume/{ticker}` | 每日場外 short-sale 成交量(flow,非未平倉)。 | `ticker`, `days=30`(1–365), `limit=200` | `list[dict]` — trade_date, short_volume, short_exempt_volume, total_volume, short_volume_percent |

### 分析與報告(有解讀)

| 工具 · 接口 | 用途 | 參數(預設) | 回傳 |
|---|---|---|---|
| **`get_analysis`** · `GET /api/analysis/{ticker}` | 四面向紅綠燈分析(基本面/籌碼/技術/期權 + 綜合)。永遠 200;缺資料面向 `verdict="na"`。 | `ticker` | `dict` — overall_verdict, lenses[4]{verdict, summary, signals[]} |
| **`get_objective_report`** · `GET /api/report/{ticker}` | 一次取客觀數據包(估值 + 三表 + 內部人 + 13F + 價格/期權摘要 + filing 標題)。純數據無評分。 | `ticker`, `sections=None`, 各塊 limit | `dict` — company + 8 個 `{source, as_of, …}` 塊 |

### 唯讀 SQL(⭐ pro tier)

直連唯讀 Postgres——唯二不打 REST API 的 tool。需 `tier:pro`。見下方 [唯讀 SQL 安全](#唯讀-sql-安全)。

| 工具 · 後端 | 用途 | 參數(預設) | 回傳 |
|---|---|---|---|
| **`execute_readonly_sql`** · 唯讀 PG pool | 跑一段唯讀 `SELECT`,做彈性/統計查詢。 | `query`(單一 SELECT) | JSON 字串 `{row_count, executed_query, rows[]}` |
| **`describe_table`** · `information_schema` | 下 SQL 前先自省 schema。 | `table_name=None`(省略 → 列出所有表) | JSON 字串 `{tables[]}` 或 `{table, columns[]}` |

## Prompts

可重用工作流(`prompts.py`),每條都要求 agent 用使用者語言回覆。

| Prompt | 用途 |
|---|---|
| **`analyze_stock(ticker)`** | 單股「股價 + 財務」簡易分析,結論優先的逐步 tool 工作流。 |
| **`analyze_stock_full(ticker)`** | 四面向(基本面/籌碼/技術/期權)紅綠燈完整報告,含缺 lens 誠實處理("based on N/4")與 13F 延遲註記。 |
| **`compare_stocks(ticker_a, ticker_b)`** | 兩檔在成長/利潤率/估值/FCF/動能上並列對照。 |
| **`build_stock_report(ticker, peers="", language=...)`** | 產出精美自包含 HTML 個股報告(四面向 + web 新聞 overlay + reverse-DCF variant perception),用 `data://report-template` 渲染。 |
| **`company_profile(ticker, language=...)`** | 白話「這家公司在幹嘛」介紹型 HTML 報告(取自 10-K Item 1),非買賣分析。 |

## Resources

| Resource | 內容 |
|---|---|
| **`data://dictionary`** | 欄位語意、單位、transaction code、filing 延遲、`null ≠ 0` 規則,並列出每個 domain 由哪些 tool 服務。解讀數字前先讀。 |
| **`data://analysis-playbook`** | `analyze_stock` 方法論的文件版,給不支援 MCP prompt 的 client。 |
| **`data://report-template`** | `build_stock_report` 用的自包含 HTML 設計系統 + 組裝指南。 |

## 唯讀 SQL 安全

`execute_readonly_sql` 接受任意 `SELECT` / `WITH … SELECT`,三層防護:

1. **SQL parsing**(`sqlparse`):只允許單一 statement;第一個有意義 keyword 必須是 `SELECT`/`WITH`;拒絕 `INSERT/UPDATE/DELETE/DROP/CREATE/ALTER/TRUNCATE/GRANT` 與敏感函數(`pg_sleep`、`pg_read_server_files`、`lo_import`、`dblink`、`COPY`…);多 statement 一律拒。
2. **DB role**:連線用 `investor_db_readonly` role,只 GRANT `SELECT`——真正的最後防線。
3. **Statement timeout**:每段查詢 5 秒上限,複雜 query 自動 abort。

- **LIMIT 注入**:查詢包成 `SELECT * FROM (<你的 SQL>) _ LIMIT n`。無頂層 `LIMIT` → `1000`;有 `LIMIT N` → `min(N, 10000)`。只認字面整數 `LIMIT`;子查詢內的 `LIMIT` 無法繞過外層上界。
- **Output 截斷**:超過 100 KB 截斷並附註記。

### 唯讀 DB role 設置(自建)

```sh
# 1. 編輯 scripts/grant_readonly.sql,把 CHANGE_ME 換成密碼。
# 2. 用 superuser 對你的 DB 跑:
psql "<superuser DSN>" -f scripts/grant_readonly.sql
# 3. 設 DSN(asyncpg 風格,不要 +asyncpg 後綴):
#    MCP_READONLY_DB_DSN=postgresql://investor_db_readonly:<密碼>@<host>:<port>/<db>
```

`MCP_READONLY_DB_DSN` 留空 = 停用這兩個 SQL tool(回 friendly error);其餘 tool 不受影響。

## 設定

| 環境變數 | 必填 | 預設 | 用途 |
|---|---|---|---|
| `MCP_API_BASE_URL` | ✅ | — | Stockfacts REST API base URL(無尾斜線)。 |
| `MCP_API_AUTH_TOKEN` | ✅ | — | Service bearer:MCP server → REST API。 |
| `MCP_BEARER_TOKEN` | — | `""` | 共用 client bearer(僅共用 token 模式)。 |
| `MCP_PER_USER_AUTH` | — | `true` | `true` = per-user SaaS 認證;`false` = 共用 token。 |
| `MCP_SAAS_DATABASE_URL` | — | `""` | SaaS 控制面 Postgres DSN(`api_keys` / `subscriptions` / `usage_events`)。要讀**也要寫**,所以不是下面那把唯讀 role。留空 = 停用 per-user 認證。 |
| `MCP_AUTH_CACHE_TTL` | — | `300` | 驗證成功的正向快取 TTL(秒);撤銷最多延遲這麼久生效。 |
| `MCP_AUTH_STALE_TTL` | — | `3600` | DB 不可用時,快取還能繼續放行多久(秒)。 |
| `MCP_QUOTA_CACHE_TTL` | — | `60` | 額度狀態快取 TTL(秒)。 |
| `MCP_READONLY_DB_DSN` | — | `""` | SQL tool 的唯讀 Postgres DSN。留空即停用。 |
| `PORT` | — | `8000` | Streamable HTTP port。 |

## 部署

任何容器平台皆可——所有平台依賴都靠環境變數注入,root `Dockerfile` 會被自動偵測。開對外 HTTP
port、設好上述環境變數即可;streamable-HTTP 接口在 `/mcp`。

## 開發

```sh
uv sync
uv run pytest -q        # 857 個測試
```

## 授權

[Apache-2.0](./LICENSE) © 2026 AAAZZZR —— Stockfacts。
