# HTML Stock-Report Skill for the MCP — Design Spec

Date: 2026-06-19
Status: approved (user said 開始)
Repo: `Trading_Agent_MCP`

## Goal

Ship an **HTML stock-report capability** to any AI agent connected to the investor-db MCP, so
they can produce a polished, self-contained HTML report that (a) is driven by the dataset map,
(b) shows **real numbers** pulled via the existing MCP tools, (c) layers in **the latest news via
the agent's own web search**, and (d) renders the final report **in the user's language** while
the skill text itself is English.

## Key finding — most of this already exists; we extend, not rebuild

The MCP already has a mature knowledge layer (`prompts.py`, `resources.py`):

- `data://dictionary` — a complete **data map**: 15 domains, each lists "which tools serve it",
  units / adjustments / codes / lags / null-semantics, real coverage figures. **This is the map.**
- `data://analysis-playbook` — document form of `analyze_stock`.
- Prompts `analyze_stock`, `analyze_stock_full` (four-lens, traffic-light, distilled from
  `stock-analysis-report/SKILL.md`), `compare_stocks` — all start with
  `Respond in the user's language`, are tool-driven, and carry the honesty rules.

So language flexibility, the map, real-number discipline, and the four-lens methodology are
**already done**. The only genuine gaps are:

1. **HTML output** — every existing prompt emits markdown/text.
2. **Web-search news overlay as a first-class step** — existing prompts only use the vendor
   `get_company_news` tool; none actively directs the agent to use its *own* web search/fetch
   for the latest news, capital-structure events, and to verify DB-only conclusions.

## What we add (surgical)

### 1. Prompt `build_stock_report(ticker, peers="", language="the user's language")` — `prompts.py`

Returns a workflow instruction string. Structure:

1. First line: `Respond in {language}.` (default = the user's language; convention preserved).
2. **Gather real numbers** — reuse the four-lens tool playbook (the exact tool names verified in
   `tools.py`): `get_company`/`search_companies`, `get_overview`, `list_daily_prices`,
   `get_income_statements`/`get_balance_sheets`/`get_cash_flow_statements`,
   `list_insider_trades`, `list_13f_top_buyers`/`list_13f_top_sellers`/`list_13f_holders`,
   `get_options_chain`/`get_option_expirations`, `list_earnings`/`list_dividends`. Honesty rules
   carried (as-of dates, null≠0, missing lens ⚪ dropped from denominator, N/4, no invented
   targets/forecasts). Points to `data://dictionary` for field semantics. Mentions the pro-tier
   `execute_readonly_sql` shortcut and the `get_objective_report` bundle.
3. **🌐 Web news overlay (first-class, NEW)**:
   - If the agent HAS web search/fetch: search (a) recent news / price-move cause, (b) latest
     earnings + management guidance, (c) capital-structure events (offering / convertible /
     going-concern), (d) verify each DB anomaly (cash-runway from a quarter-end snapshot,
     anomalous Form 4, missing earnings date). Each item cited with source + date. (Mirrors
     `stock-analysis-report/SKILL.md` Lens 5.)
   - If NO web tools: fall back to the MCP `get_company_news` tool and **state explicitly in the
     report that live web news was omitted**.
4. **Render to HTML** — read `data://report-template`, copy its `<style>` verbatim, and assemble
   the body from its component patterns with the real values. Output **one self-contained HTML
   document** (inline CSS, no external assets). A compact structural outline is embedded inline as
   a fallback for clients that cannot read resources.
5. **`peers` non-empty** → also emit the head-to-head comparison section (one card per ticker +
   a comparison table). One prompt covers single + comparison (no separate `compare_stocks_html`).

### 2. Resource `data://report-template` — `resources.py`

An English HTML "design system" string (not a rigid fill-in template, so it flexes for 1..N
stocks and a variable lens count):

- The full `<style>` block (cobalt theme, traffic-light classes `.g/.y/.r/.w`, KPI grid, aspect
  blocks, `.cmp` comparison table, `.warn`/`.intro` callouts, sources, disclaimer, print rules).
- Documented HTML snippet patterns: page skeleton, header ribbon, verdict banner (`.vg/.vy/.vr`),
  light row, KPI cell, aspect block with traffic-light dot, warn/caveat box, comparison table,
  sources list, disclaimer.
- Assembly instructions: section order (header → verdict+lights → KPI → 4 lenses → news → events
  → sources/freshness/disclaimer), where the 🟢🟡🔴⚪ colors map to CSS classes, and the rule that
  every number carries an as-of date and missing data is marked ⚪.

### 3. `server.py` — update `instructions`

Add `build_stock_report` to the prompts list and `data://report-template` to the resources list
in the agent-facing `instructions` string so agents discover them.

## How each requirement is met

| Requirement | Mechanism |
|---|---|
| Database map | reuse `data://dictionary` (exists) + prompt step 2 names the tools |
| Real numbers | tool playbook + existing honesty rules; placeholders/memory forbidden |
| Latest news | NEW first-class web-search overlay (web-present / web-absent branches) |
| Report language | `Respond in {language}`; skill body English |
| HTML output | NEW prompt + NEW `data://report-template` resource |
| Installed in MCP for other agents | it IS a prompt + resource — any MCP client can invoke/read |

## Files touched (MCP repo only)

- `src/trading_agent_mcp/prompts.py` — +1 prompt (`build_stock_report`)
- `src/trading_agent_mcp/resources.py` — +1 resource (`data://report-template`)
- `src/trading_agent_mcp/server.py` — `instructions` +2 references
- `tests/test_knowledge.py` — +registration + content-anchor tests for the new prompt & resource

## Test plan (TDD, mirrors existing `test_knowledge.py`)

- `build_stock_report` registered in `list_prompts()`.
- `data://report-template` registered in `list_resources()`.
- Rendered prompt: line 1 starts `Respond in`; contains the ticker; contains web-overlay both
  branches, the `data://report-template` reference, the HTML "self-contained" instruction, honesty
  anchors (N/4, 45-day), and (with `peers`) the comparison instruction.
- Resource text: contains `<style>`, traffic-light classes, KPI/aspect/comparison patterns,
  disclaimer.
- Full suite: `uv run pytest -q` stays green.

## Out of scope

- No new tools, no API/ETL changes, no schema changes.
- Not modifying the four-lens methodology (reuse `analyze_stock_full` / dictionary).
- Deployment (Zeabur) is the owner's existing redeploy flow; no infra change needed —
  prompts/resources ship with the code.

## Decisions (defaults locked, user did not override)

1. Prompt name = `build_stock_report` (verb_noun, consistent with `analyze_stock`).
2. Single prompt + `peers` param covers single & comparison (YAGNI; no second prompt).
3. Template delivered as `data://report-template` resource + compact inline fallback in the prompt.

## Research-informed refinements (3-agent web/GitHub study, 2026-06-19)

Three Opus agents studied GitHub finance skills (InvestSkill, buffett-skills, TradingAgents,
FinRobot, claude-trading-skills, borghei, sec-edgar-mcp…), sell-side/equity-research report
anatomy + scoring/accessibility, and the first-party-data-moat / no-real-time framing. Adopted
into the prompt + template (only what fits our honesty rules and the data we actually have):

**Report shape**
- BLUF: lead with a verdict card (overall traffic-light + "based on N/4 lenses"), then evidence.
- Per-lens **signal = bullish / neutral / bearish + confidence**, but **NO Buy/Sell action and NO
  invented price target** (conflicts with our honesty rule + "not advice"). Surface lens
  disagreement as signal, don't average it into mush.
- **Top-3 risks + a "What would change this view" box**; lesser detail to an appendix.

**First-party data is the hero (our moat + anti-slop)**
- Order the signature panels Insider (Form 4 cluster buying, C-suite-weighted, P/S only) →
  Institutional (13F QoQ new/exit/>25% deltas + crowding, ~45-day lag stated as a feature with
  the Buffett-cloning evidence) → Options (IV rank / 25-delta skew / put-call / max pain as an
  EOD *positioning* read — our greeks history is the rare asset) → SEC-grounded financials
  (normalized, multi-currency, cite the filing). Prices/macro are supporting context, not headline.
- Replace any price target with the **options-implied move / IV / skew** (data-derived, honest).
- Cross-link datasets into one narrative no single retail tool can produce.

**"No real-time" = deliberate scope, stated confidently near the top**
- Header line: built for fundamental/ownership/positioning analysis (multi-day to multi-quarter),
  EOD + filing-based, not an intraday execution tool.
- **As-of stamp per dataset**: Form 4 transaction-date vs disclosed-date; 13F quarter-end vs
  filing-date; T+1 close; last-EOD options.
- **Provenance line**: "sourced & normalized directly from SEC EDGAR + primary feeds, not scraped."

**Presentation (folded into `data://report-template`)**
- Typography: **Inter for prose + JetBrains Mono for every number**. Cobalt base + semantic
  green/red/amber matching the traffic lights.
- **Never color alone** — pair every 🟢🟡🔴⚪ with an icon/word; WCAG 4.5:1; grayscale/print-safe.
- Sticky sidebar TOC, mobile breakpoint, print CSS.
- Charts: **zero-dependency by default** (inline SVG sparklines, CSS bars); Chart.js 4.4.0 via CDN
  listed as an OPTIONAL upgrade only (honors "justify new dependencies" — never forced).

**Web-news overlay discipline**
- Keep web news in a clearly-labelled "Recent news (web; may post-date our EOD data)" section,
  each item tagged sentiment + source + timestamp; it must NOT contaminate the first-party cited
  core. Also used to verify DB anomalies (cash runway, anomalous Form 4, missing earnings date).

**Self-check gate before emitting HTML**
- Enforce "missing lens → dropped from denominator → N/4"; every claim must trace to an on-page
  number (Damodaran Possible→Plausible→Probable); no single-scenario DCF; no invented target.

These are folded into the prompt text and the template; they do not change the file list above.
