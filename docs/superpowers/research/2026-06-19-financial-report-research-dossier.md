# Financial-Report Skill — Research Dossier (2026-06-19)

Raw collected research backing the `build_stock_report` prompt + `data://report-template` resource.
Three parallel Opus subagents did the data collection; this file preserves WHAT they collected
(repos, articles, URLs, facts) so the source material is reviewable, not just the synthesis in the
design spec.

## Method

| Agent | Mission | agentId | web tool calls | subagent tokens | duration |
|---|---|---|---|---|---|
| 1 | GitHub finance skills / prompt libs / MCP servers | `a91a98106c84b089f` | 25 | 64,145 | ~256 s |
| 2 | Equity-research report anatomy / scoring / layout / trust | `a2a2f3eaa5e548ded` | 20 | 55,590 | ~205 s |
| 3 | First-party data moat + no-real-time framing | `a1f369a076f0c1bc7` | 18 | 58,150 | ~195 s |

Total ≈ 63 web searches/fetches, ≈178k subagent tokens, ≈11 min combined.

---

## Agent 1 — GitHub finance skills / MCP (collected)

Repos found (name — URL — signal — borrowable takeaways):

- **yennanliu/InvestSkill** — https://github.com/yennanliu/InvestSkill — 86★, v1.6.0, 288+ tests.
  Most relevant (does HTML stock reports from a Claude skill). Self-contained HTML design system
  (navy `#0F172A`, sky `#0284C7`, teal `#0D9488`, Inter + JetBrains Mono, **Chart.js 4.4.0 only CDN
  dep**, all CSS/JS inline). Standardized per-section "Investment Signal Block"
  (Signal/Confidence/Horizon/Score/Action/Conviction). Cover page with overall score first →
  sticky sidebar nav → 15 modules → final weighted scorecard + bull/bear → disclaimer. Same HTML →
  PDF via print/wkhtmltopdf/Playwright. Has a `result-validator` skill (scores data quality /
  methodology / signal consistency).
- **agi-now/buffett-skills** — https://github.com/agi-now/buffett-skills — 624★. Best
  conclusion-first output template: Conclusion → Circle of Competence → Assumptions → Business
  Quality (5 moat types) → Financial Snapshot → Valuation (3-scenario, margin-of-safety) →
  Sell-Criteria → **top-3 Risks only** → Monitoring → Verdict. Quick-screen vs deep-analysis
  dispatch (progressive disclosure). Published eval: +skill 100% vs 66.7% pass, ~+30% tokens.
- **TauricResearch/TradingAgents** — https://github.com/TauricResearch/TradingAgents — 87.2k★.
  Four-lens analyst split (Fundamentals/Sentiment/News/Technical ≈ our lenses). Adversarial
  bull-vs-bear debate before verdict. Anti-hallucination: verified data snapshots / pinned
  analysis date prevent hallucinated prices (relevant to our EOD constraint).
- **AI4Finance-Foundation/FinRobot** — https://github.com/AI4Finance-Foundation/FinRobot — 7.3k★.
  Equity-research report generator; 8 specialized agents; HTML+PDF with 15+ chart types; DCF +
  3-yr projections + peer comps; ships example reports (NVDA/MSFT/TSLA/META).
- **OctagonAI/skills** — https://github.com/OctagonAI/skills — 125★. master→subordinate
  orchestration; taxonomy Financial Metrics/Earnings Call/SEC Filings/Market Data; names
  Altman Z-Score, Piotroski Score.
- **tradermonty/claude-trading-skills** — https://github.com/tradermonty/claude-trading-skills —
  2.0k★, 49 skills. SKILL.md layout: frontmatter + `references/` + `scripts/` + `assets/`. Named
  methods: CANSLIM, Minervini VCP, 5-factor earnings-reaction. Explicit EOD-vs-real-time + graceful
  degradation (CSV fallback).
- **borghei/claude-skills → finance/financial-analyst/SKILL.md** —
  https://github.com/borghei/claude-skills/blob/main/finance/financial-analyst/SKILL.md — best
  honesty rules: 20 ratios in 5 buckets; anti-patterns (single-scenario DCF, terminal growth ≥ GDP,
  stale WACC, generic benchmarks, "treating model output as final"); "executive summary leads with
  the decision"; materiality → appendix; every assumption gets source + last-reviewed date.
- **stefanoamorelli/sec-edgar-mcp** — https://github.com/stefanoamorelli/sec-edgar-mcp — 321★.
  Verification-first: exact numeric precision + every response includes the SEC filing URL.
- **cyanheads/secedgar-mcp-server** — https://github.com/cyanheads/secedgar-mcp-server — v0.11.0.
  Dedup to one value per period; trigram near-match suggestions; XBRL friendly-concept mapping.
- **himself65/finance-skills** — https://github.com/himself65/finance-skills — 2.8k★. company-valuation
  (DCF + relative + SOTP), earnings-preview/recap, options-payoff (interactive charts), stock-liquidity
  (Amihud), sepa-strategy. Markdown + interactive HTML/SVG widgets.
- **RKiding/Awesome-finance-skills** — https://github.com/RKiding/Awesome-finance-skills — 2.5k★.
  Report pipeline Plan → Write → Edit → Chart; signal-tracker (strengthen/weaken/falsify); sentiment -1..+1.
- **dgunning/edgartools** — https://github.com/dgunning/edgartools — cleanest Python ref for
  10-K/8-K/XBRL/Form 3-4-5/13F parsing; correctness oracle.

---

## Agent 2 — Equity-research report best practices (collected)

Report anatomy (sell-side convergence, conclusion-first): identity strip → BLUF "Key Takeaways"
verdict → score/lens panel → investment thesis → catalysts → lens deep-dives → valuation/financial
exhibits → risks → provenance + disclaimer footer. Sources: [Wall Street Prep](https://www.wallstreetprep.com/knowledge/sample-equity-research-report/),
[CFI](https://corporatefinanceinstitute.com/resources/valuation/equity-research-report/),
[Mergers & Inquisitions](https://mergersandinquisitions.com/equity-research-report/),
[BLUF/Wikipedia](https://en.wikipedia.org/wiki/BLUF_(communication)).

Scoring/rating: [Stockopedia StockRanks](https://www.stockopedia.com/stockranks/) (Quality/Value/Momentum
each 0–100 → composite; "StockRank Styles" 2×2; explicitly tells users small rank differences are
noise, high scores "stack the odds" not guarantee), [GuruFocus GF Score](https://www.gurufocus.com/term/gf-score)
(5 dims → 0–100). Show components not just composite; surface lens disagreement (Value vs Momentum
often negatively correlated — [CI Global](https://www.cifinancial.com/ci-gam/ca/en/expert-insights/articles/value-momentum-etf.html),
[Alpha Architect](https://alphaarchitect.com/cross-section-of-returns/)).

Presentation/layout: KPI cards top, grouped by decision area ([ChartsWatcher](https://chartswatcher.com/pages/blog/7-top-financial-dashboard-examples-for-2025-success),
[Fanruan](https://gallery.fanruan.com/kpi-card-example)); inline sparklines ([Tufte](https://kevinjmagnan.com/2021/04/05/2021-03-31-Tufte-mini-blog-1.html));
chart→metric mapping (line=time series, **bar=peer comps**, **waterfall=decomposition**, tables=exact comps;
avoid pie) ([wpDataTables](https://wpdatatables.com/financial-charts-graphs/), [HiBob waterfall](https://www.hibob.com/financial-tools/financial-waterfall-charts/));
**never color alone** — pair RAG with icon/shape/label, WCAG 4.5:1, grayscale-safe ([Sigma](https://www.sigmacomputing.com/blog/data-charts-color-blindness),
[Datylon](https://www.datylon.com/blog/data-visualization-for-visually-impaired-users), [EU data-viz guide](https://data.europa.eu/apps/data-visualisation-guide/accessible-colours));
maximize data-ink, small multiples ([GeeksforGeeks/Tufte](https://www.geeksforgeeks.org/data-visualization/mastering-tuftes-data-visualization-principles/)).

Trust/honesty: missing data → exclude don't impute ([Chicago Booth](https://www.chicagobooth.edu/review/better-way-finance-others-handle-missing-data),
[QuantPedia](https://quantpedia.com/how-to-deal-with-missing-financial-data/)); Damodaran 3P narrative test
Possible→Plausible→Probable ([Damodaran](https://seanmaguinness.substack.com/p/narratives-and-numbers-a-beginners));
disclaimer essentials + data-delay disclosure ([Stock Analysis disclaimer](https://stockanalysis.com/data-disclaimer/),
[Accounting Insights](https://accountinginsights.org/investment-disclaimer-key-points-you-need-to-know/)); CFA "reasonable basis"
([CFA V(A)](https://www.cfainstitute.org/standards/professionals/code-ethics-standards/standards-of-practice-v-a));
AI hallucinates plausible metrics → must ground in retrieved data, AI research less informative w/o expert steering
([Baytech](https://www.baytechconsulting.com/blog/hidden-dangers-of-ai-hallucinations-in-financial-services),
[TradingCentral](https://www.tradingcentral.com/blog/hallucination-in-ai-why-it-is-risky-for-investors---and-how-we-solved-this-problem-with-fibi),
[HBS WP 25-055](https://www.hbs.edu/ris/download.aspx?name=25-055.pdf), [SSRN](https://dx.doi.org/10.2139/ssrn.5226562)).

---

## Agent 3 — First-party data moat & no-real-time framing (collected)

Why first-party wins: proprietary normalized datasets are "effectively irreplaceable" moats
([Morgan Stanley](https://www.morganstanley.com/im/en-us/financial-advisor/insights/global-equity-observer/when-every-data-business-looks-like-a-target.html),
[Primary VC](https://www.primary.vc/articles/the-data-moats-that-unlock-billion-dollar-fintech-outcomes));
alt data fills the "information void between earnings calls" ([getaura](https://blog.getaura.ai/alternative-data),
[Similarweb](https://www.similarweb.com/blog/investor/asset-research/hedge-funds-use-alternative-data/));
yfinance/scraped is unreliable & raw XBRL isn't comparable without normalization — normalization IS the product
([Tobi Lux](https://medium.com/@Tobi_Lux/data-from-yfinance-some-observations-41e99d768069),
[Trading Dude](https://medium.com/@trading.dude/why-yfinance-keeps-getting-blocked-and-what-to-use-instead-92d84bb2cc01),
[Intrinio XBRL](https://intrinio.com/blog/normalized-xbrl-data), [Daloopa](https://daloopa.com/), [XBRL US](https://xbrl.us/why-normalize-data/)).

How the best present each dataset: insider = cluster buying (3+ execs / ~60d, C-suite weighted, filter 10b5-1)
([MarketTriage](https://markettriage.com/insider-trading-signals), [GuruFocus](https://www.gurufocus.com/insider/summary),
[Fintel](https://fintel.io/insider-trading-data), [Quiver](https://www.quiverquant.com/insiders/)); 13F = new/exit/>25%
deltas + crowding, lag as a feature — Buffett 13F clone with 45-day lag still beat S&P ~10.75%/yr 1976–2006
([Quantpedia](https://quantpedia.com/strategies/alpha-cloning-following-13f-fillings), [WhaleWisdom](https://whalewisdom.com/),
[sec-api 13F](https://sec-api.io/datasets/form-13f-holdings)); options = IV surface / 25-delta skew / put-call / max pain /
GEX ([FlashAlpha](https://flashalpha.com/stock/xlk), [SpotGamma](https://spotgamma.com/gamma-exposure-gex/)) — our edge is the
full historical EOD greeks series (SpotGamma sells intraday/0DTE we don't claim); financials = cell-level traceable to filing.

EOD vs real-time: EOD sufficient for long-term/fundamental/ownership/positioning/swing
([Intrinio 15-min delay](https://intrinio.com/blog/understanding-the-impact-of-15-minute-delayed-stock-prices-on-market-analysis),
[FinFeedAPI](https://www.finfeedapi.com/blog/stock-market-data-realtime-intraday-historical)); real-time needed for
intraday execution/day trading/0DTE ([godeldiscount](https://godeldiscount.com/blog/realtime-vs-delayed-market-data)) — name
those honestly; frame delay as deliberate cost/scope tradeoff, stamp every dataset's as-of.

Gaps in retail tools to exploit: no free tool has complete/customizable Form 4 coverage
([Form4SEC](https://form4sec.com/sec-form-4-a-comprehensive-guide-for-investors/)); OpenInsider/Quiver lack
cluster+sentiment; free 13F rarely shows filer-level QoQ deltas + crowding; full options greeks *history* is
essentially unavailable to retail; multi-currency ADR financials mangled by commodity sources.

---

## Cross-agent synthesis → adopted into the skill

(See the design spec `2026-06-19-html-report-skill-design.md` §"Research-informed refinements".)
BLUF verdict card first + N/4; per-lens signal bullish/neutral/bearish **without Buy/Sell or invented
target** (use options-implied/skew); first-party panels as hero (Insider→13F→Options→Financials), cited
to filings; "no real-time" stated as deliberate scope + per-dataset as-of stamps + EDGAR provenance line;
Inter + JetBrains Mono, color-never-alone, print/mobile CSS, zero-dep default (Chart.js optional); web news
isolated in its own labelled section + used to verify DB anomalies; self-check gate (N/4, every claim ties
to an on-page number, Damodaran 3P, no single-scenario DCF).
