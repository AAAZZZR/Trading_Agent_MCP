# Buy-side / Institutional Equity-Report Craft — and how to do it on EOD, first-party data

Deep-research synthesis (2026-06-19; 102 agents, 20 sources fetched, 25 claims adversarially
verified 3-vote, 24 confirmed). Primary sources: CFA Institute Standards of Practice (12th ed.),
Damodaran (NYU Stern), CFA Enterprising Investor. This is the reference that backs the institutional
upgrade to `build_stock_report`.

## 1. Where the edge actually is — *variant perception*

Alpha comes from the **divergence between your view and the market's consensus**, not from valuation
mechanics. CFA Institute (Katsenelson): *"Alpha is not in your cash-flow estimates. It's not in your
discount rates. And it's not in your cheap multiples … Divergence between your perception and that of
the market is where … true alpha comes from."* ([CFA](https://rpc.cfainstitute.org/blogs/enterprising-investor/2015/the-five-dimensions-of-variant-perception))

Implication for us: **a platform with no analyst consensus is NOT deprived of the source of edge** —
it just has to surface a *differentiated, defensible reading of first-party data* (insider clusters,
13F flow, options skew/IV, margin/FCF trajectory) **against what the price implies**. Caveat (verified
refutation): "just be different" is wrong — divergence is *necessary but not sufficient*; the view must
also be correct, so always state **what would make it wrong**.

## 2. Valuation — ~85% of real research is RELATIVE; every multiple is a compressed DCF

Damodaran: *"Almost 85% of equity research reports are based upon a multiple and comparables"*, and
*"embedded in every multiple are all of the variables that drive every discounted cash flow valuation
— growth, risk and cash flow patterns."* ([Damodaran](https://pages.stern.nyu.edu/~adamodar/pdfiles/country/relvalFMA.pdf))
So relative / own-history valuation is the legitimate spine, and a multiple can be inverted to reveal
fundamentals. (Direction is solid; the precise "85%" is a teaching rule-of-thumb.)

## 3. The honest target substitute — reverse-DCF / market-implied growth

Justified (intrinsic) multiples derive algebraically from a Gordon-growth DCF using the firm's **own
observable fundamentals**, and the same equations **invert** to back out the growth the market price
is embedding:

- **P/E** = Payout × (1+g) / (r − g)
- **P/B** = (ROE − g) / (r − g)   [≡ ROE×Payout×(1+g)/(r−g), via g = (1−Payout)×ROE]
- **P/S** = NetMargin × Payout × (1+g) / (r − g)

Solve any of these for **g** → **market-implied growth**. Present as *"at today's multiple, the market
is pricing in ~X% growth — is that plausible vs the company's own 5-yr history and sector?"* This is
the rigorous, honest replacement for a fabricated price target. **Label it "implied/justified GIVEN
fundamentals," never a forecast.** Honest use requires disclosing the assumed discount rate `r` and
controlling for growth/risk differences; relative valuation can carry market-wide mispricing (right vs
peers, still wrong absolutely).

## 4. Scenarios — base/bull/bear from first-party data

CFA V(A) on quant models: *span "a wide range of possible input expectations, including negative market
events."* Build three scenarios with **disclosed** inputs, not vibes:
- **base** = reverse-DCF implied growth at the current multiple;
- **bull / bear** = the company's own ~5-yr valuation-multiple percentile band (re-rate / de-rate) and
  the trajectory of first-party signals;
- size the move with the **options-implied move** (ATM straddle ≈ 1σ to the next event; price×IV×√(DTE/365))
  and read **put/call skew** as the market's own asymmetry. Always include a downside.

## 5. The CFA reasonable-basis + communication spine (adopt as norms, not compliance)

**V(A) Diligence & Reasonable Basis** — governs PROCESS quality (a sound conclusion via a sloppy
process still fails). Rigor checklist to consider: macro conditions; the company's operating/financial
history; sector & **business-cycle stage**; the **output AND limitations** of quant models; **peer-group
appropriateness**. For quant models (reverse-DCF / options-implied / scenarios): understand
assumptions+limits, **test the output before using it**, and **span a downside**. Third-party inputs:
vet by assumptions/rigor/timeliness/objectivity and **weight first-party SEC filings above unvetted
aggregators/blogs/social**. ([CFA V(A)](https://www.cfainstitute.org/standards/professionals/code-ethics-standards/standards-of-practice-v-a))

**V(B) Communication** — **distinguish fact from opinion**: any estimate / future price is an *opinion
subject to future circumstances* — write *"implies / we expect / we read,"* never *"will."* For complex
quant, *separate fact from statistical conjecture*, identify limitations, and *use caution promoting a
model's accuracy — output is an estimate, not a certainty.* **Disclose the method and its significant
limitations** — for us that means stating the gaps: **no analyst consensus, EOD/not real-time, 13F
~45-day lag, the assumed `r`.** ([CFA V(B)](https://www.cfainstitute.org/standards/professionals/code-ethics-standards/standards-of-practice-V-B))

> These are duties owed by human CFA members; we adopt them as **best-practice norms** for an honest
> constrained-data report (principle-level, not a compliance claim).

## 6. What we can / can't do — substitution table

| Institutional element | We lack | Honest substitute (have) |
|---|---|---|
| Consensus / estimate revisions | no analyst dataset | first-party signal divergence (insider/13F/options) vs price |
| Forward EPS → forward P/E | no consensus EPS | trailing + **market-implied growth** (reverse-DCF) |
| Analyst price target | (must not fabricate) | **options-implied move / IV / skew**; justified-multiple value range |
| Real-time tape / intraday flow | EOD only | EOD options positioning (OI, skew, max pain), volume-vs-OI |
| Live ownership | 13F ~45-day lag | stamp as-of; Form 4 is ~T+2 (near-current) |
| Forward revenue model | no estimates | own ~5-yr trend + reverse-DCF plausibility check |

## 7. How this maps into `build_stock_report`

Encoded into the prompt (2026-06-19):
1. **Thesis = variant perception**: state *market-implied vs our first-party read* + **what would make
   us wrong** — up top, as the report's spine.
2. **Valuation**: show **market-implied growth (reverse-DCF)** with the disclosed `r`, plus each
   multiple's **own 5-yr percentile** — not a fabricated target.
3. **Scenarios**: base/bull/bear with disclosed inputs + the options-implied move; always a downside.
4. **Honesty spine**: fact-vs-opinion wording; a "How we analyze & its limits" disclosure (no
   consensus / EOD / 13F lag / assumed r); first-party SEC weighted above web; stress a negative case.

Open items the research did not pin down (build from principles, flag as lower-evidence): exact
bank house-format section order; a canonical options-implied-move recipe; scenario probability weights;
the "right" `r` with no consensus cost-of-capital (disclose + stress it).
