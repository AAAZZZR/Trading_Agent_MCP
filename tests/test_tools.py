"""tools.py 的單元測試 —— 每個 @mcp.tool 呼叫對的 URL + params。

策略:用 respx 攔截 module-level singleton `api` 的 httpx call,驗:
  1. 所有 HTTP-backed tool 都註冊到 mcp。
  2. 每個 tool 對應正確的 API endpoint + 方法 + 預設 params。
  3. ticker 自動 upper-case。
"""

import pytest
import respx

from trading_agent_mcp import tools
from trading_agent_mcp.server import mcp

# ---- 註冊性測試 -----------------------------------------------------------


async def test_all_tools_registered() -> None:
    """server 啟動時 tools 模組被 import,所有 @mcp.tool 都掛上去。

    用 provider 層 list_tools(未經 auth 過濾):mcp.list_tools() 會對
    execute_readonly_sql / describe_table 做 tier:pro gating,無 auth context 時會藏起來。
    """
    registered = await mcp._local_provider.list_tools()
    names = {t.name for t in registered}

    expected = {
        "get_data_coverage",
        "list_companies", "search_companies", "get_company",
        "list_filings", "get_filing", "list_filing_sections", "get_filing_section",
        "get_income_statements", "get_balance_sheets", "get_cash_flow_statements",
        "get_latest_period",
        "list_insider_trades",
        "list_daily_prices", "get_latest_price", "list_hourly_prices",
        "list_institutional_holders", "get_holders_breakdown",
        "list_13f_holders", "list_13f_portfolio",
        "list_13f_top_buyers", "list_13f_top_sellers",
        "list_dividends", "list_splits",
        "list_earnings", "get_earnings_calendar",
        "get_etf_profile", "list_etf_holdings", "list_etfs_holding_ticker",
        "list_macro_series", "get_macro_series",
        "screen_insider_buys",
        "get_analysis", "get_objective_report", "get_overview",
        "get_options_chain", "get_option_expirations", "get_option_contract_history",
        "search_institutions", "get_institution", "list_etf_sectors",
        "execute_readonly_sql", "describe_table",
    }
    assert names == expected, f"missing: {expected - names}, extra: {names - expected}"


# ---- 個別 tool URL / params 測試 ------------------------------------------
#
# 每個 case 都跑同樣的 pattern:mock 對應 endpoint → call tool → assert URL+params。
# 用 parametrize 一次跑 15 個。
#
# (tool_callable, expected_path, kwargs_to_call_with, expected_query_params)


_CASES = [
    # Meta / coverage
    (tools.get_data_coverage, "/api/meta/coverage", {}, {}),

    # Companies(MCP 端預設分頁 limit=200 / offset=0)
    (tools.list_companies, "/api/companies", {}, {"limit": "200", "offset": "0"}),
    (
        tools.list_companies,
        "/api/companies",
        {"limit": 50, "offset": 100},
        {"limit": "50", "offset": "100"},
    ),
    (
        tools.search_companies,
        "/api/companies/search",
        {"q": "apple"},
        {"q": "apple", "limit": "20"},
    ),
    (
        tools.search_companies,
        "/api/companies/search",
        {"q": "nvda", "limit": 5},
        {"q": "nvda", "limit": "5"},
    ),
    (tools.get_company, "/api/companies/AAPL", {"ticker": "aapl"}, {}),

    # Filings
    (
        tools.list_filings,
        "/api/filings",
        {"ticker": "aapl"},
        {"ticker": "AAPL", "limit": "50"},
    ),
    (
        tools.list_filings,
        "/api/filings",
        {"ticker": "aapl", "form_type": "10-K", "limit": 5},
        {"ticker": "AAPL", "form_type": "10-K", "limit": "5"},
    ),
    (tools.get_filing, "/api/filings/0000320193-24-000123", {"accession": "0000320193-24-000123"}, {}),
    (tools.list_filing_sections, "/api/filings/X/sections", {"accession": "X"}, {}),
    (tools.get_filing_section, "/api/filings/X/sections/Item%201A", {"accession": "X", "item_code": "Item 1A"}, {}),

    # Financials
    (
        tools.get_income_statements,
        "/api/financials/income",
        {"ticker": "msft"},
        {"ticker": "MSFT", "limit": "20"},
    ),
    (
        tools.get_income_statements,
        "/api/financials/income",
        {"ticker": "msft", "period": "annual", "limit": 5},
        {"ticker": "MSFT", "period": "annual", "limit": "5"},
    ),
    (
        tools.get_balance_sheets,
        "/api/financials/balance",
        {"ticker": "nvda", "period": "quarterly"},
        {"ticker": "NVDA", "period": "quarterly", "limit": "20"},
    ),
    (
        tools.get_cash_flow_statements,
        "/api/financials/cashflow",
        {"ticker": "tsla"},
        {"ticker": "TSLA", "limit": "20"},
    ),
    (
        tools.get_latest_period,
        "/api/financials/latest",
        {"ticker": "aapl", "period": "annual"},
        {"ticker": "AAPL", "period": "annual"},
    ),

    # Insider
    (
        tools.list_insider_trades,
        "/api/insider",
        {"ticker": "aapl", "since": "2024-01-01"},
        {"ticker": "AAPL", "limit": "100", "since": "2024-01-01"},
    ),

    # Prices
    (
        tools.list_daily_prices,
        "/api/prices/daily",
        {"ticker": "aapl", "start": "2024-01-01", "end": "2024-12-31"},
        {"ticker": "AAPL", "limit": "2000", "start": "2024-01-01", "end": "2024-12-31"},
    ),
    (
        tools.get_latest_price,
        "/api/prices/daily/latest",
        {"ticker": "aapl"},
        {"ticker": "AAPL"},
    ),
    (
        tools.list_hourly_prices,
        "/api/prices/hourly",
        {"ticker": "aapl"},
        {"ticker": "AAPL", "limit": "5000"},
    ),
    (
        tools.list_hourly_prices,
        "/api/prices/hourly",
        {
            "ticker": "aapl",
            "start": "2026-05-22T13:30:00Z",
            "end": "2026-05-22T20:00:00Z",
            "limit": 100,
        },
        {
            "ticker": "AAPL",
            "limit": "100",
            "start": "2026-05-22T13:30:00Z",
            "end": "2026-05-22T20:00:00Z",
        },
    ),

    # Holdings
    (
        tools.list_institutional_holders,
        "/api/holdings/institutions",
        {"ticker": "aapl"},
        {"ticker": "AAPL"},
    ),
    (
        tools.get_holders_breakdown,
        "/api/holdings/major",
        {"ticker": "aapl"},
        {"ticker": "AAPL"},
    ),

    # 13F (first-party SEC)
    (
        tools.list_13f_holders,
        "/api/13f/holders",
        {"ticker": "aapl"},
        {"ticker": "AAPL", "limit": "100"},
    ),
    (
        tools.list_13f_holders,
        "/api/13f/holders",
        {"ticker": "aapl", "quarter_end": "2024-12-31", "limit": 10},
        {"ticker": "AAPL", "quarter_end": "2024-12-31", "limit": "10"},
    ),
    (
        tools.list_13f_portfolio,
        "/api/13f/portfolio",
        {"cik": "0001067983"},
        {"cik": "0001067983", "limit": "100"},
    ),
    (
        tools.list_13f_top_buyers,
        "/api/13f/top-buyers",
        {"ticker": "aapl"},
        {"ticker": "AAPL", "limit": "50"},
    ),
    (
        tools.list_13f_top_sellers,
        "/api/13f/top-sellers",
        {"ticker": "aapl"},
        {"ticker": "AAPL", "limit": "50"},
    ),

    # Corporate actions (dividends / splits)
    (
        tools.list_dividends,
        "/api/dividends/AAPL",
        {"ticker": "aapl"},
        {"limit": "100", "offset": "0"},
    ),
    (
        tools.list_dividends,
        "/api/dividends/AAPL",
        {"ticker": "aapl", "limit": 10, "offset": 20},
        {"limit": "10", "offset": "20"},
    ),
    (
        tools.list_splits,
        "/api/splits/MSFT",
        {"ticker": "msft"},
        {"limit": "100", "offset": "0"},
    ),

    # Earnings calendar
    (
        tools.list_earnings,
        "/api/earnings/AAPL",
        {"ticker": "aapl"},
        {"limit": "100"},
    ),
    (
        tools.list_earnings,
        "/api/earnings/AAPL",
        {"ticker": "aapl", "start": "2026-01-01", "end": "2026-06-30", "limit": 50},
        {"limit": "50", "start": "2026-01-01", "end": "2026-06-30"},
    ),
    (
        tools.get_earnings_calendar,
        "/api/earnings/calendar",
        {},
        {"limit": "500"},
    ),
    (
        tools.get_earnings_calendar,
        "/api/earnings/calendar",
        {"start": "2026-06-01", "end": "2026-06-07", "limit": 100},
        {"limit": "100", "start": "2026-06-01", "end": "2026-06-07"},
    ),

    # ETF
    (
        tools.get_etf_profile,
        "/api/etf/SPY/profile",
        {"ticker": "spy"},
        {},
    ),
    (
        tools.list_etf_holdings,
        "/api/etf/SPY/holdings",
        {"ticker": "spy"},
        {"limit": "100", "offset": "0"},
    ),
    (
        tools.list_etf_holdings,
        "/api/etf/SPY/holdings",
        {"ticker": "spy", "limit": 25, "offset": 50},
        {"limit": "25", "offset": "50"},
    ),
    (
        tools.list_etfs_holding_ticker,
        "/api/etf/holders/AAPL",
        {"ticker": "aapl"},
        {"limit": "100", "offset": "0"},
    ),

    # Macro
    (
        tools.list_macro_series,
        "/api/macro/series",
        {},
        {},
    ),
    (
        tools.list_macro_series,
        "/api/macro/series",
        {"category": "commodity"},
        {"category": "commodity"},
    ),
    # MCP 端 order 預設 desc(agent 多半要最新值);畫圖 / 算 MA 才傳 asc。
    (
        tools.get_macro_series,
        "/api/macro/series/CPI",
        {"series_id": "CPI"},
        {"limit": "2000", "order": "desc"},
    ),
    (
        tools.get_macro_series,
        "/api/macro/series/TREASURY_YIELD_10YEAR",
        {"series_id": "TREASURY_YIELD_10YEAR", "start": "2020-01-01", "limit": 500},
        {"limit": "500", "order": "desc", "start": "2020-01-01"},
    ),
    (
        tools.get_macro_series,
        "/api/macro/series/INDEX_VIX",
        {"series_id": "INDEX_VIX", "limit": 1, "order": "asc"},
        {"limit": "1", "order": "asc"},
    ),

    # Screener (cross-ticker)
    (
        tools.screen_insider_buys,
        "/api/screener/insider-buys",
        {},
        {"transaction_code": "P", "since_days": "90", "limit": "100"},
    ),
    (
        tools.screen_insider_buys,
        "/api/screener/insider-buys",
        {"insider_title_contains": "CEO", "market_cap_min": 1000000.0, "since_days": 30},
        {
            "transaction_code": "P",
            "since_days": "30",
            "limit": "100",
            "insider_title_contains": "CEO",
            "market_cap_min": "1000000.0",
        },
    ),

    # Analysis / Report / Overview
    (tools.get_analysis, "/api/analysis/AAPL", {"ticker": "aapl"}, {}),
    (
        tools.get_objective_report,
        "/api/report/AAPL",
        {"ticker": "aapl"},
        {"statements_limit": "8", "insider_limit": "20", "holders_limit": "10",
         "filings_limit": "5", "recent_price_bars": "30"},
    ),
    (tools.get_overview, "/api/overview/AAPL", {"ticker": "aapl"}, {}),

    # Options(工具參數統一成 ticker;但 API 端 query 仍是 underlying)
    (
        tools.get_options_chain,
        "/api/options/chain",
        {"ticker": "aapl"},
        {"underlying": "AAPL", "limit": "250"},
    ),
    (
        tools.get_options_chain,
        "/api/options/chain",
        {"ticker": "aapl", "as_of": "2026-06-03", "expiration": "2026-06-03",
         "option_type": "call", "limit": 5},
        {"underlying": "AAPL", "limit": "5", "as_of": "2026-06-03",
         "expiration": "2026-06-03", "option_type": "call"},
    ),
    (
        tools.get_option_expirations,
        "/api/options/expirations",
        {"ticker": "aapl"},
        {"underlying": "AAPL"},
    ),
    (
        tools.get_option_contract_history,
        "/api/options/contract/AAPL260605C00200000",
        {"contract_id": "AAPL260605C00200000"},
        {"limit": "2000"},
    ),

    # 13F institutions
    (
        tools.search_institutions,
        "/api/13f/institutions/search",
        {"q": "berkshire"},
        {"q": "berkshire", "limit": "20"},
    ),
    (
        tools.get_institution,
        "/api/13f/institutions/0001067983",
        {"cik": "0001067983"},
        {},
    ),

    # ETF sectors
    (tools.list_etf_sectors, "/api/etf/SPY/sectors", {"ticker": "spy"}, {}),
]


@pytest.mark.parametrize(("tool_fn", "expected_path", "call_kwargs", "expected_query"), _CASES)
async def test_tool_calls_correct_endpoint(
    tool_fn, expected_path: str, call_kwargs: dict, expected_query: dict
) -> None:
    """每個 tool 應該打到對應的 API path 與正確的 query params。"""
    fake_response = [] if expected_path.endswith(("companies", "filings", "income", "balance", "cashflow", "insider", "prices", "daily", "hourly", "institutions", "sections", "sectors", "expirations")) else {}

    with respx.mock(base_url="http://test-api") as mock:
        # 因為 @mcp.tool 包裝過,直接呼叫 tool_fn.fn 才是底層 async function;
        # 但 FastMCP 的 tool 物件可被當 callable 直接 await(運行期 LLM 也是這條路)。
        route = mock.get(expected_path).respond(200, json=fake_response)
        await tool_fn.fn(**call_kwargs) if hasattr(tool_fn, "fn") else await tool_fn(**call_kwargs)

    assert route.called, f"{tool_fn} did not call {expected_path}"
    sent = route.calls.last.request
    actual_params = dict(sent.url.params)
    assert actual_params == expected_query, (
        f"params mismatch for {tool_fn}: expected {expected_query}, got {actual_params}"
    )
