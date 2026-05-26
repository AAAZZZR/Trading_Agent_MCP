"""tools.py 的單元測試 —— 每個 @mcp.tool 呼叫對的 URL + params。

策略:用 respx 攔截 module-level singleton `api` 的 httpx call,驗:
  1. 15 個 tool 都註冊到 mcp。
  2. 每個 tool 對應正確的 API endpoint + 方法 + 預設 params。
  3. ticker 自動 upper-case。
"""

import pytest
import respx

from trading_agent_mcp import tools
from trading_agent_mcp.server import mcp

# ---- 註冊性測試 -----------------------------------------------------------


async def test_all_tools_registered() -> None:
    """server 啟動時 tools 模組被 import,16 個 @mcp.tool 都掛上去。"""
    registered = await mcp.list_tools()
    names = {t.name for t in registered}

    expected = {
        "list_companies", "get_company",
        "list_filings", "get_filing", "list_filing_sections", "get_filing_section",
        "get_income_statements", "get_balance_sheets", "get_cash_flow_statements",
        "get_latest_period",
        "list_insider_trades",
        "list_daily_prices", "get_latest_price",
        "list_institutional_holders", "get_holders_breakdown",
        "execute_readonly_sql",
    }
    assert names == expected, f"missing: {expected - names}, extra: {names - expected}"


# ---- 個別 tool URL / params 測試 ------------------------------------------
#
# 每個 case 都跑同樣的 pattern:mock 對應 endpoint → call tool → assert URL+params。
# 用 parametrize 一次跑 15 個。
#
# (tool_callable, expected_path, kwargs_to_call_with, expected_query_params)


_CASES = [
    # Companies
    (tools.list_companies, "/api/companies", {}, {}),
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
        "/api/prices",
        {"ticker": "aapl", "start": "2024-01-01", "end": "2024-12-31"},
        {"ticker": "AAPL", "limit": "1000", "start": "2024-01-01", "end": "2024-12-31"},
    ),
    (
        tools.get_latest_price,
        "/api/prices/latest",
        {"ticker": "aapl"},
        {"ticker": "AAPL"},
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
]


@pytest.mark.parametrize(("tool_fn", "expected_path", "call_kwargs", "expected_query"), _CASES)
async def test_tool_calls_correct_endpoint(
    tool_fn, expected_path: str, call_kwargs: dict, expected_query: dict
) -> None:
    """每個 tool 應該打到對應的 API path 與正確的 query params。"""
    fake_response = [] if expected_path.endswith(("companies", "filings", "income", "balance", "cashflow", "insider", "prices", "institutions", "sections")) else {}

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
