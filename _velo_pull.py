# temp: pull a four-lens data bundle for "velo" THROUGH the MCP server (stdio),
# computing compact aggregates so the analyst (me) reads a small JSON, not raw bars.
import asyncio
import json
import statistics
from pathlib import Path

from fastmcp import Client

REPO = str(Path(__file__).parent)
CONFIG = {
    "mcpServers": {
        "investor-db": {
            "command": "uv",
            "args": ["run", "python", "-m", "trading_agent_mcp", "--stdio"],
            "cwd": REPO,
        }
    }
}

OUT = {}


def data_of(res):
    if getattr(res, "data", None) is not None:
        return res.data
    try:
        return json.loads(res.content[0].text)
    except Exception:
        return res.content[0].text if res.content else None


async def call(client, tool, args):
    try:
        return data_of(await client.call_tool(tool, args)), None
    except Exception as e:
        return None, f"{type(e).__name__}: {str(e)[:200]}"


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


async def main():
    async with Client(CONFIG) as client:
        # --- resolve ---
        cands, err = await call(client, "search_companies", {"q": "velo", "limit": 10})
        OUT["search"] = {"candidates": cands, "error": err}
        ticker = None
        if isinstance(cands, list) and cands:
            exact = [c for c in cands if str(c.get("ticker", "")).upper() == "VELO"]
            ticker = (exact[0] if exact else cands[0])["ticker"]
        OUT["resolved_ticker"] = ticker
        if not ticker:
            print(json.dumps(OUT, ensure_ascii=False, indent=1, default=str))
            return

        company, err = await call(client, "get_company", {"ticker": ticker})
        OUT["company"] = company or err
        overview, err = await call(client, "get_overview", {"ticker": ticker})
        OUT["overview"] = overview or err

        # --- price state (one call, compute everything here) ---
        prices, err = await call(
            client, "list_daily_prices",
            {"ticker": ticker, "start": "2025-06-01", "limit": 300},
        )
        if isinstance(prices, list) and prices:
            closes = [f(p.get("close")) for p in prices]
            vols = [f(p.get("volume")) or 0 for p in prices]
            last = prices[-1]
            c = closes[-1]

            def ret(n):
                return round((c / closes[-1 - n] - 1) * 100, 1) if len(closes) > n and closes[-1 - n] else None

            hi, lo = max(closes), min(closes)
            ma50 = statistics.mean(closes[-50:]) if len(closes) >= 50 else None
            ma200 = statistics.mean(closes[-200:]) if len(closes) >= 200 else None
            avg_vol50 = statistics.mean(vols[-50:]) if len(vols) >= 50 else None
            OUT["price"] = {
                "as_of": last.get("date"), "close": c, "bars_1y": len(prices),
                "ret_1m_pct": ret(21), "ret_3m_pct": ret(63), "ret_6m_pct": ret(126),
                "ret_1y_pct": ret(len(closes) - 1) if len(closes) > 200 else None,
                "hi_52w": hi, "lo_52w": lo,
                "pos_52w_pct": round((c - lo) / (hi - lo) * 100, 1) if hi != lo else None,
                "ma50": round(ma50, 2) if ma50 else None,
                "ma200": round(ma200, 2) if ma200 else None,
                "vol_last": vols[-1], "vol_avg50": round(avg_vol50) if avg_vol50 else None,
            }
        else:
            OUT["price"] = {"error": err or "empty"}

        # --- financial trajectory (quarterly) ---
        inc, err = await call(client, "get_income_statements",
                              {"ticker": ticker, "period": "quarterly", "limit": 9})
        OUT["income_q"] = [
            {k: r.get(k) for k in ("fiscal_year", "fiscal_period", "period_end",
                                   "revenue", "gross_profit", "operating_income",
                                   "net_income", "eps_diluted", "shares_diluted")}
            for r in inc] if isinstance(inc, list) else (err or inc)
        cf, err = await call(client, "get_cash_flow_statements",
                             {"ticker": ticker, "period": "quarterly", "limit": 8})
        OUT["cashflow_q"] = [
            {k: r.get(k) for k in ("fiscal_period", "period_end", "operating_cash_flow",
                                   "capex", "free_cash_flow")}
            for r in cf] if isinstance(cf, list) else (err or cf)
        bal, err = await call(client, "get_balance_sheets",
                              {"ticker": ticker, "period": "quarterly", "limit": 2})
        OUT["balance_q"] = [
            {k: r.get(k) for k in ("fiscal_period", "period_end", "cash_and_equivalents",
                                   "short_term_investments", "long_term_debt",
                                   "short_term_debt", "total_current_assets",
                                   "total_current_liabilities", "total_equity")}
            for r in bal] if isinstance(bal, list) else (err or bal)

        # --- ownership ---
        ins, err = await call(client, "list_insider_trades",
                              {"ticker": ticker, "since": "2025-12-11", "limit": 100})
        if isinstance(ins, list):
            ps = [t for t in ins if t.get("transaction_code") in ("P", "S")]
            buys = [t for t in ps if t["transaction_code"] == "P"]
            sells = [t for t in ps if t["transaction_code"] == "S"]
            OUT["insider_6m"] = {
                "buy_tx": len(buys), "sell_tx": len(sells),
                "distinct_buyers": len({t.get("insider_name") for t in buys}),
                "buy_value": sum(f(t.get("total_value")) or 0 for t in buys),
                "sell_value": sum(f(t.get("total_value")) or 0 for t in sells),
                "recent": [{k: t.get(k) for k in ("transaction_date", "insider_name",
                                                  "insider_title", "transaction_code",
                                                  "shares", "total_value")}
                           for t in ps[:8]],
            }
        else:
            OUT["insider_6m"] = {"error": err}

        h13, err = await call(client, "list_13f_holders", {"ticker": ticker, "limit": 50})
        if isinstance(h13, list) and h13:
            qe = h13[0].get("quarter_end")
            adding = [h for h in h13 if h.get("change_type") in ("new", "increase")]
            trimming = [h for h in h13 if h.get("change_type") in ("decrease", "sold_out")]
            OUT["holders_13f"] = {
                "quarter_end": qe, "holders_sampled": len(h13),
                "adding": len(adding), "trimming": len(trimming),
                "top5": [{k: h.get(k) for k in ("filer_name", "shares", "market_value",
                                                "change_type", "change_in_shares")}
                         for h in h13[:5]],
            }
        else:
            OUT["holders_13f"] = {"error": err or "empty"}
        brk, err = await call(client, "get_holders_breakdown", {"ticker": ticker})
        OUT["holders_breakdown"] = brk or err

        # --- options ---
        exps, err = await call(client, "get_option_expirations", {"ticker": ticker})
        OUT["option_expirations"] = exps if exps else (err or "none")
        chain, err = await call(client, "get_options_chain", {"ticker": ticker})
        if isinstance(chain, list) and chain:
            def tot(typ, field):
                return sum(f(o.get(field)) or 0 for o in chain if o.get("option_type") == typ)
            pv, cv = tot("put", "volume"), tot("call", "volume")
            poi, coi = tot("put", "open_interest"), tot("call", "open_interest")
            OUT["options"] = {
                "contracts": len(chain), "as_of": chain[0].get("date"),
                "pc_volume": round(pv / cv, 2) if cv else None,
                "pc_oi": round(poi / coi, 2) if coi else None,
                "total_volume": pv + cv, "total_oi": poi + coi,
            }
        elif isinstance(chain, dict):
            OUT["options"] = {"raw_keys": list(chain.keys())}
        else:
            OUT["options"] = {"error": err or "empty"}

        # --- events + macro context ---
        earn, err = await call(client, "list_earnings", {"ticker": ticker, "limit": 5})
        OUT["earnings"] = earn if earn else (err or "none")
        div, err = await call(client, "list_dividends", {"ticker": ticker, "limit": 5})
        OUT["dividends"] = div if div else (err or "none")
        vix, err = await call(client, "get_macro_series",
                              {"series_id": "INDEX_VIX", "limit": 1})
        OUT["vix"] = vix if vix else err

    Path("_velo_data.json").write_text(
        json.dumps(OUT, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    print(json.dumps(OUT, ensure_ascii=True, indent=1, default=str)[:6000])


asyncio.run(main())
