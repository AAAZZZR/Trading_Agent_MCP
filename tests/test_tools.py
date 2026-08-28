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
        "start_here",
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
        "get_earnings_transcript",
        "get_company_news", "get_market_news",
        "get_etf_profile", "list_etf_holdings", "list_etfs_holding_ticker",
        "list_macro_series", "get_macro_series",
        "get_market_movers", "get_ipo_calendar",
        "screen_insider_buys",
        "get_analysis", "get_objective_report", "get_overview",
        "get_short_interest", "get_short_volume", "screen_high_short_interest",
        "get_short_interest_movers", "get_dark_pool_weekly",
        "get_options_chain", "get_option_expirations", "get_option_contract_history",
        "search_institutions", "get_institution", "list_etf_sectors",
        "execute_readonly_sql", "describe_table",
    }
    assert names == expected, f"missing: {expected - names}, extra: {names - expected}"


async def test_all_tools_annotated_readonly() -> None:
    """全站唯讀:每個 tool 都必須標 readOnlyHint=True + openWorldHint=False。

    MCP client(與 Anthropic Connectors Directory 審查)靠這組 hint 判斷 tool 是否安全;
    新 tool 漏帶 `annotations=_READONLY_ANNOTATIONS` 時這裡會擋下。
    """
    registered = await mcp._local_provider.list_tools()
    for t in registered:
        assert t.annotations is not None, f"{t.name} 缺 annotations"
        assert t.annotations.readOnlyHint is True, f"{t.name} 未標 readOnlyHint=True"
        assert t.annotations.openWorldHint is False, f"{t.name} 未標 openWorldHint=False"


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
    # 注意:list_hourly_prices / list_daily_prices 不帶 start/end 時會自動回推 start
    # (讓預設回「最近 N 筆」),那條路徑的參數含動態日期,改由下方專屬測試驗證,
    # 這裡只保留「顯式帶 start/end 時不回推」的 case。
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

    # News + sentiment(get_earnings_transcript 是多 call,另測;這裡放單 call 的 news)
    (
        tools.get_company_news,
        "/api/companies/AAPL/news",
        {"ticker": "aapl"},
        {"days": "7", "min_relevance": "0.5", "limit": "20"},
    ),
    (
        tools.get_company_news,
        "/api/companies/AAPL/news",
        {"ticker": "aapl", "days": 30, "min_relevance": 0.7, "limit": 5},
        {"days": "30", "min_relevance": "0.7", "limit": "5"},
    ),
    (
        tools.get_market_news,
        "/api/market/news",
        {},
        {"limit": "20"},
    ),
    (
        tools.get_market_news,
        "/api/market/news",
        {"topic": "earnings", "limit": 50},
        {"limit": "50", "topic": "earnings"},
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

    # Market snapshot (movers / IPO calendar)
    (
        tools.get_market_movers,
        "/api/market/movers",
        {},
        {},
    ),
    (
        tools.get_market_movers,
        "/api/market/movers",
        {"date": "2026-06-11"},
        {"date": "2026-06-11"},
    ),
    (
        tools.get_ipo_calendar,
        "/api/market/ipo-calendar",
        {},
        {},
    ),
    (
        tools.get_ipo_calendar,
        "/api/market/ipo-calendar",
        {"from_date": "2026-06-01", "to_date": "2026-09-01"},
        {"from_date": "2026-06-01", "to_date": "2026-09-01"},
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

    # FINRA short market data
    (
        tools.get_short_interest,
        "/api/shorts/interest/WLF",
        {"ticker": "wlf"},
        {"limit": "24"},
    ),
    (
        tools.get_short_volume,
        "/api/shorts/volume/WLF",
        {"ticker": "wlf"},
        {"days": "30", "limit": "200"},
    ),
    (
        tools.screen_high_short_interest,
        "/api/shorts/screener",
        {},
        {
            "min_short_percent_float": "10.0",
            "min_days_to_cover": "0.0",
            "limit": "50",
        },
    ),
    (
        tools.get_short_interest_movers,
        "/api/shorts/movers",
        {},
        {"direction": "increase", "min_position": "100000", "limit": "25"},
    ),
    (
        tools.get_short_interest_movers,
        "/api/shorts/movers",
        {"direction": "decrease", "min_position": 250_000,
         "market_cap_min": 1e9, "market_cap_max": 5e10, "limit": 10},
        {"direction": "decrease", "min_position": "250000", "limit": "10",
         "market_cap_min": "1000000000.0", "market_cap_max": "50000000000.0"},
    ),
    (
        tools.get_dark_pool_weekly,
        "/api/shorts/darkpool/WLF",
        {"ticker": "wlf"},
        {"weeks": "12"},
    ),
    (
        tools.get_dark_pool_weekly,
        "/api/shorts/darkpool/WLF",
        {"ticker": "wlf", "weeks": 4},
        {"weeks": "4"},
    ),

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


# ---- 新 market tool 的 error 路徑 -----------------------------------------
#
# 後端非 2xx 由 api.get 的單一咽喉點翻成 ToolError(完整翻譯規則在 test_api_client.py
# 測過);這裡只確認兩個新 tool 確實走那條路、不外洩裸 httpx 例外。

from fastmcp.exceptions import ToolError  # noqa: E402


def _unwrap(tool_fn):
    """@mcp.tool 可能(視 FastMCP 版本)留 `.fn` 指向底層 async function;
    沒有就直接用本體 —— 對齊上面參數化測試的呼叫方式。"""
    return tool_fn.fn if hasattr(tool_fn, "fn") else tool_fn


async def test_get_market_movers_404_raises_tool_error() -> None:
    """movers 帶了無資料的日期 → 後端 404 → ToolError(帶 status/path 前綴)。"""
    with respx.mock(base_url="http://test-api") as mock:
        mock.get("/api/market/movers").respond(
            404, json={"detail": "No market movers for date 1990-01-01."}
        )
        with pytest.raises(ToolError) as excinfo:
            await _unwrap(tools.get_market_movers)(date="1990-01-01")
    assert "404" in str(excinfo.value)


async def test_get_ipo_calendar_500_raises_tool_error() -> None:
    """IPO 行事曆遇上游 5xx → ToolError,不外洩裸 httpx 例外。"""
    with respx.mock(base_url="http://test-api") as mock:
        mock.get("/api/market/ipo-calendar").respond(500, text="boom")
        with pytest.raises(ToolError) as excinfo:
            await _unwrap(tools.get_ipo_calendar)()
    assert "500" in str(excinfo.value)


# ---- FINRA movers / darkpool(回傳原樣轉發 + error 走同一咽喉點)-------------


async def test_get_short_interest_movers_forwards_payload() -> None:
    """movers 是薄轉發:後端的 {items, count} 原樣回給 agent,不在 MCP 端加工。"""
    payload = {
        "items": [
            {
                "ticker": "WLF",
                "company_name": "Wildflower Inc",
                "settlement_date": "2026-08-14",
                "current_short_position": 5_000_000,
                "previous_short_position": 3_000_000,
                "change_previous": 2_000_000,
                "change_percent": "66.67",
                "days_to_cover": "4.2",
                "float_shares": None,
                "short_percent_float": None,
                "market_cap": 1.2e9,
            }
        ],
        "count": 1,
    }
    with respx.mock(base_url="http://test-api") as mock:
        mock.get("/api/shorts/movers").respond(200, json=payload)
        result = await _unwrap(tools.get_short_interest_movers)()
    assert result == payload


async def test_get_dark_pool_weekly_forwards_payload() -> None:
    """darkpool 同樣薄轉發;分母不可用時 pct_of_volume 為 null,MCP 端不補 0。"""
    payload = [
        {
            "week_start": "2026-07-27",
            "ats_share_qty": "1200000",
            "ats_trade_count": 4200,
            "otc_share_qty": "3400000",
            "otc_trade_count": 9100,
            "pct_of_volume": None,
            "top_venues": [
                {"mpid": "UBSA", "firm_name": "UBS ATS", "share_quantity": "500000"}
            ],
        }
    ]
    with respx.mock(base_url="http://test-api") as mock:
        mock.get("/api/shorts/darkpool/WLF").respond(200, json=payload)
        result = await _unwrap(tools.get_dark_pool_weekly)(ticker="wlf")
    assert result == payload


async def test_get_dark_pool_weekly_unknown_ticker_returns_empty_list() -> None:
    """FINRA 沒涵蓋的代號回空 list(非 404),tool 不該把它翻成錯誤。"""
    with respx.mock(base_url="http://test-api") as mock:
        mock.get("/api/shorts/darkpool/NOSUCH").respond(200, json=[])
        result = await _unwrap(tools.get_dark_pool_weekly)(ticker="nosuch")
    assert result == []


async def test_get_short_interest_movers_422_raises_tool_error() -> None:
    """direction / limit 超出後端值域 → 422 → ToolError,不外洩裸 httpx 例外。"""
    with respx.mock(base_url="http://test-api") as mock:
        mock.get("/api/shorts/movers").respond(
            422, json={"detail": "limit must be <= 500"}
        )
        with pytest.raises(ToolError) as excinfo:
            await _unwrap(tools.get_short_interest_movers)(limit=9999)
    assert "422" in str(excinfo.value)


async def test_get_dark_pool_weekly_500_raises_tool_error() -> None:
    """darkpool 遇上游 5xx → ToolError。"""
    with respx.mock(base_url="http://test-api") as mock:
        mock.get("/api/shorts/darkpool/WLF").respond(500, text="boom")
        with pytest.raises(ToolError) as excinfo:
            await _unwrap(tools.get_dark_pool_weekly)(ticker="wlf")
    assert "500" in str(excinfo.value)


# ---- get_earnings_transcript(多 call:先 list 再 detail)--------------------


async def test_get_earnings_transcript_explicit_quarter() -> None:
    """帶 quarter:先打 list(挑 available_quarters),再打該季 detail。"""
    with respx.mock(base_url="http://test-api") as mock:
        list_route = mock.get("/api/companies/AAPL/transcripts").respond(
            200,
            json=[
                {"quarter": "2025Q4", "segments": 71, "fetched_at": "2026-02-01T12:00:00Z"},
                {"quarter": "2025Q3", "segments": 68, "fetched_at": "2025-11-01T12:00:00Z"},
            ],
        )
        detail_route = mock.get("/api/companies/AAPL/transcripts/2025Q3").respond(
            200,
            json={
                "ticker": "AAPL",
                "quarter": "2025Q3",
                "segments_total": 68,
                "fetched_at": "2025-11-01T12:00:00Z",
                "segments": [{"seq": 0, "speaker": "Tim Cook", "speaker_title": "CEO",
                              "content": "...", "sentiment": "0.4"}],
            },
        )
        result = await _unwrap(tools.get_earnings_transcript)(
            ticker="aapl", quarter="2025q3", limit=10
        )

    assert list_route.called
    assert detail_route.called
    # 預設 limit=40,這裡顯式帶 10;offset 預設 0。
    sent = detail_route.calls.last.request
    assert dict(sent.url.params) == {"offset": "0", "limit": "10"}
    assert result["quarter"] == "2025Q3"
    assert result["available_quarters"] == ["2025Q4", "2025Q3"]


async def test_get_earnings_transcript_omitted_quarter_picks_latest() -> None:
    """quarter 省略:自動取 list 第一筆(最新季)當 target。"""
    with respx.mock(base_url="http://test-api") as mock:
        mock.get("/api/companies/AAPL/transcripts").respond(
            200,
            json=[
                {"quarter": "2025Q4", "segments": 71, "fetched_at": "2026-02-01T12:00:00Z"},
            ],
        )
        latest_route = mock.get("/api/companies/AAPL/transcripts/2025Q4").respond(
            200,
            json={"ticker": "AAPL", "quarter": "2025Q4", "segments_total": 71,
                  "fetched_at": "2026-02-01T12:00:00Z", "segments": []},
        )
        result = await _unwrap(tools.get_earnings_transcript)(ticker="aapl")

    assert latest_route.called
    # 預設 limit=40 protect context。
    sent = latest_route.calls.last.request
    assert dict(sent.url.params) == {"offset": "0", "limit": "40"}
    assert result["available_quarters"] == ["2025Q4"]


async def test_get_earnings_transcript_no_transcripts_raises_tool_error() -> None:
    """公司無任何逐字稿(list 回空)→ ToolError 誠實說無資料,不去打 detail。"""
    with respx.mock(base_url="http://test-api") as mock:
        list_route = mock.get("/api/companies/SMALLCO/transcripts").respond(200, json=[])
        with pytest.raises(ToolError) as excinfo:
            await _unwrap(tools.get_earnings_transcript)(ticker="smallco")
    assert list_route.called
    assert "No earnings call transcripts" in str(excinfo.value)


async def test_get_earnings_transcript_speaker_filter_forwarded() -> None:
    """speaker 過濾轉成 detail 的 query param。"""
    with respx.mock(base_url="http://test-api") as mock:
        mock.get("/api/companies/AAPL/transcripts").respond(
            200,
            json=[{"quarter": "2025Q4", "segments": 71, "fetched_at": "2026-02-01T12:00:00Z"}],
        )
        detail_route = mock.get("/api/companies/AAPL/transcripts/2025Q4").respond(
            200,
            json={"ticker": "AAPL", "quarter": "2025Q4", "segments_total": 71,
                  "fetched_at": "2026-02-01T12:00:00Z", "segments": []},
        )
        await _unwrap(tools.get_earnings_transcript)(
            ticker="aapl", speaker="cook", offset=40
        )

    sent = detail_route.calls.last.request
    assert dict(sent.url.params) == {"offset": "40", "limit": "40", "speaker": "cook"}


async def test_get_company_news_404_raises_tool_error() -> None:
    """company news 後端 404 → ToolError(走 api.get 咽喉點)。"""
    with respx.mock(base_url="http://test-api") as mock:
        mock.get("/api/companies/NOPE/news").respond(404, json={"detail": "Unknown ticker"})
        with pytest.raises(ToolError) as excinfo:
            await _unwrap(tools.get_company_news)(ticker="nope")
    assert "404" in str(excinfo.value)


# ---- prices:不帶 start/end 時的 auto 視窗 —— 釘「最新一根一定在」這個不變量 --------
#
# 後端語意是 `ORDER BY date/dt ASC` + `LIMIT`,也就是回視窗內**最舊**的 N 筆。舊實作把
# 視窗壓到「剛好約 limit 個交易日」,只要視窗內 bar 數多於 limit,最新那幾根就被後端從頭
# 截斷、靜默丟掉 —— 而且舊測試是把同一條回推公式抄過來當斷言(套套邏輯),等於把 bug
# 鎖住。這裡改成:自己搭一個忠實模擬後端(ASC 產生視窗內所有 bar,再套 `[:limit]`),
# 直接斷言「回傳的最後一筆 == 模擬宇宙裡最新的那一根」。回推公式怎麼調都不影響這組測試。

from datetime import UTC, date, datetime, timedelta  # noqa: E402
from unittest.mock import patch  # noqa: E402

import httpx  # noqa: E402

# 2026-08-17 是週一 → 這 7 天剛好覆蓋週一到週日,用來模擬「今天是星期幾」。
_WEEK = [date(2026, 8, 17) + timedelta(days=i) for i in range(7)]
_HOURLY_BAR_HOURS = (14, 15, 16, 17, 18, 19)  # UTC;實測每交易日就是這 6 根


def _freeze_today(today: date):
    """把 tools 模組裡的 `date.today()` 釘死在 `today`(讓星期幾可被 parametrize)。"""

    class _FrozenDate(date):
        @classmethod
        def today(cls) -> date:
            return today

    return patch.object(tools, "date", _FrozenDate)


def _freeze_now(now: datetime):
    """把 tools 模組裡的 `datetime.now()` 釘死在 `now`。"""

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None) -> datetime:
            return now

    return patch.object(tools, "datetime", _FrozenDatetime)


def _latest_weekday(today: date) -> date:
    """模擬宇宙裡最新的那個交易日(週末往前退到週五)。"""
    d = today
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _fake_daily_backend(today: date):
    """忠實模擬 /api/prices/daily:視窗內所有平日 ASC 產生,再套 `[:limit]`。

    `[:limit]` 就是後端 `ORDER BY date ASC LIMIT n` 的語意 —— 視窗內 bar 多於 limit 時
    被砍掉的是**最新**那幾根。工具若還是把使用者的小 limit 直接送給後端就會在這裡露餡。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        start = date.fromisoformat(params["start"])
        end = date.fromisoformat(params["end"]) if "end" in params else today
        limit = int(params["limit"])
        rows = []
        cursor = start
        while cursor <= end:
            if cursor.weekday() < 5:
                rows.append({"ticker": "AAPL", "date": cursor.isoformat(), "close": 1.0})
            cursor += timedelta(days=1)
        return httpx.Response(200, json=rows[:limit])

    return handler


def _fake_hourly_backend(now: datetime):
    """忠實模擬 /api/prices/hourly:視窗內每個平日 6 根 bar,ASC,再套 `[:limit]`。

    過濾條件比照後端:`start <= dt <= now`(比的是時間點,不是日期)。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        start = datetime.strptime(params["start"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
        limit = int(params["limit"])
        rows = []
        cursor = start.date()
        while cursor <= now.date():
            if cursor.weekday() < 5:
                for hour in _HOURLY_BAR_HOURS:
                    dt = datetime(
                        cursor.year, cursor.month, cursor.day, hour, tzinfo=UTC
                    )
                    if start <= dt <= now:
                        rows.append(
                            {
                                "ticker": "AAPL",
                                "dt": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "close": 1.0,
                            }
                        )
            cursor += timedelta(days=1)
        return httpx.Response(200, json=rows[:limit])

    return handler


@pytest.mark.parametrize("today", _WEEK, ids=lambda d: d.strftime("%a"))
@pytest.mark.parametrize("limit", [*range(1, 41), 100, 2000])
async def test_list_daily_prices_auto_window_returns_newest_n(
    limit: int, today: date
) -> None:
    """auto 視窗必須回**最新** N 筆:最後一筆 == 宇宙裡最新的交易日,長度 == limit。

    順帶釘死 fetch 用的是 API 上限(而不是使用者的 limit)—— 那正是「後端不會截斷」
    這個結構性保證的來源。
    """
    with respx.mock(base_url="http://test-api") as mock:
        route = mock.get("/api/prices/daily").mock(side_effect=_fake_daily_backend(today))
        with _freeze_today(today):
            result = await _unwrap(tools.list_daily_prices)(ticker="aapl", limit=limit)

    params = dict(route.calls.last.request.url.params)
    assert params["limit"] == str(tools._DAILY_FETCH_MAX)  # (c) 用 API 上限抓
    assert "end" not in params  # 上界留白 = 取到最新
    assert result[-1]["date"] == _latest_weekday(today).isoformat()  # (a) 最新一定在
    assert len(result) == limit  # (b) 資料足夠時剛好 N 筆


@pytest.mark.parametrize("today", _WEEK, ids=lambda d: d.strftime("%a"))
@pytest.mark.parametrize("limit", [*range(1, 31), 5000])
async def test_list_hourly_prices_auto_window_returns_newest_n(
    limit: int, today: date
) -> None:
    """小時 K 的 auto 視窗同樣必須回**最新** N 根(每交易日 6 根)。"""
    now = datetime(today.year, today.month, today.day, 23, 30, tzinfo=UTC)
    latest = _latest_weekday(today)
    expected_newest = datetime(
        latest.year, latest.month, latest.day, _HOURLY_BAR_HOURS[-1], tzinfo=UTC
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    with respx.mock(base_url="http://test-api") as mock:
        route = mock.get("/api/prices/hourly").mock(side_effect=_fake_hourly_backend(now))
        with _freeze_now(now):
            result = await _unwrap(tools.list_hourly_prices)(ticker="aapl", limit=limit)

    params = dict(route.calls.last.request.url.params)
    assert params["limit"] == str(tools._HOURLY_FETCH_MAX)
    assert "end" not in params
    assert result[-1]["dt"] == expected_newest
    assert len(result) == limit


async def test_list_daily_prices_keeps_explicit_bounds() -> None:
    """顯式帶 start/end:原樣轉發使用者的 limit,且**不做** client 端裁切。

    自帶區間 = 後端語意(該區間內最舊 N 筆),工具不能偷偷改成「最新 N 筆」——
    那會讓「查 2024 上半年最前面 5 筆」這種明確請求拿到錯的東西。
    """
    payload = [
        {"ticker": "AAPL", "date": "2024-01-02", "close": 1.0},
        {"ticker": "AAPL", "date": "2024-01-03", "close": 2.0},
        {"ticker": "AAPL", "date": "2024-01-04", "close": 3.0},
    ]
    with respx.mock(base_url="http://test-api") as mock:
        route = mock.get("/api/prices/daily").respond(200, json=payload)
        result = await _unwrap(tools.list_daily_prices)(
            ticker="aapl", start="2024-01-01", end="2024-06-30", limit=30
        )

    params = dict(route.calls.last.request.url.params)
    assert params["start"] == "2024-01-01"
    assert params["end"] == "2024-06-30"
    assert params["limit"] == "30"  # 送使用者的 limit,不是 API 上限
    assert result == payload  # 原樣回傳,不裁切


async def test_list_hourly_prices_keeps_explicit_bounds() -> None:
    """小時 K 自帶 start/end 時同樣原樣轉發 limit、不裁切。"""
    payload = [
        {"ticker": "AAPL", "dt": "2026-05-22T14:00:00Z", "close": 1.0},
        {"ticker": "AAPL", "dt": "2026-05-22T15:00:00Z", "close": 2.0},
    ]
    with respx.mock(base_url="http://test-api") as mock:
        route = mock.get("/api/prices/hourly").respond(200, json=payload)
        result = await _unwrap(tools.list_hourly_prices)(
            ticker="aapl",
            start="2026-05-22T13:30:00Z",
            end="2026-05-22T20:00:00Z",
            limit=100,
        )

    params = dict(route.calls.last.request.url.params)
    assert params["limit"] == "100"
    assert result == payload


async def test_list_daily_prices_auto_window_returns_all_when_data_short() -> None:
    """視窗內 bar 數 < limit(新上市 / 資料不足)→ 全給,不報錯也不補空。"""
    payload = [
        {"ticker": "NEWCO", "date": "2026-08-17", "close": 1.0},
        {"ticker": "NEWCO", "date": "2026-08-18", "close": 2.0},
    ]
    with respx.mock(base_url="http://test-api") as mock:
        mock.get("/api/prices/daily").respond(200, json=payload)
        result = await _unwrap(tools.list_daily_prices)(ticker="newco", limit=50)

    assert result == payload


@pytest.mark.parametrize("limit", [1, 2, 3])
async def test_list_daily_prices_auto_window_survives_weekend_gap(limit: int) -> None:
    """小 limit 遇上「週一早上、日 K 還沒刷新」不可以回空 list。

    釘的是 `_MIN_DAILY_WINDOW_DAYS` 這個視窗下限:純比例回推在 limit=1 時只往回 2 天,
    視窗 [週六, 週一] 內一根 bar 都沒有(週末無盤 + 週一尚未刷新)→ 回空。墊到 7 天才
    跨得過週末拿到上週五那根。視窗給寬不花成本(fetch 用 API 上限、多的在 client 端裁掉)。
    """
    monday = date(2026, 8, 17)
    last_friday = date(2026, 8, 14)

    def handler(request: httpx.Request) -> httpx.Response:
        """模擬「最新一根停在上週五」的後端(週一盤前尚未刷新)。"""
        start = date.fromisoformat(request.url.params["start"])
        rows = []
        cursor = start
        while cursor <= last_friday:
            if cursor.weekday() < 5:
                rows.append({"ticker": "AAPL", "date": cursor.isoformat(), "close": 1.0})
            cursor += timedelta(days=1)
        return httpx.Response(200, json=rows[: int(request.url.params["limit"])])

    with respx.mock(base_url="http://test-api") as mock:
        mock.get("/api/prices/daily").mock(side_effect=handler)
        with _freeze_today(monday):
            result = await _unwrap(tools.list_daily_prices)(ticker="aapl", limit=limit)

    assert result, "視窗塌到跨不過週末 → 回空 list"
    assert result[-1]["date"] == last_friday.isoformat()
    assert len(result) == limit


async def test_get_latest_price_returns_backend_payload() -> None:
    """get_latest_price 原樣回後端 payload(單筆 dict,不做任何加工)。"""
    payload = {
        "ticker": "AAPL",
        "date": "2026-08-20",
        "open": 1.0,
        "high": 2.0,
        "low": 0.5,
        "close": 1.5,
        "volume": 100,
    }
    with respx.mock(base_url="http://test-api") as mock:
        route = mock.get("/api/prices/daily/latest").respond(200, json=payload)
        result = await _unwrap(tools.get_latest_price)(ticker="aapl")

    assert dict(route.calls.last.request.url.params) == {"ticker": "AAPL"}
    assert result == payload


# ---- get_earnings_transcript:falsy / whitespace quarter 視同省略 -----------------


async def test_get_earnings_transcript_blank_quarter_picks_latest() -> None:
    """quarter 傳空字串 → 視同省略,取最新一季(不能被當成合法季別去打 detail)。"""
    with respx.mock(base_url="http://test-api") as mock:
        mock.get("/api/companies/AAPL/transcripts").respond(
            200,
            json=[{"quarter": "2025Q4", "segments": 71, "fetched_at": "2026-02-01T12:00:00Z"}],
        )
        latest_route = mock.get("/api/companies/AAPL/transcripts/2025Q4").respond(
            200,
            json={"ticker": "AAPL", "quarter": "2025Q4", "segments_total": 71,
                  "fetched_at": "2026-02-01T12:00:00Z", "segments": []},
        )
        result = await _unwrap(tools.get_earnings_transcript)(ticker="aapl", quarter="")

    assert latest_route.called
    assert result["quarter"] == "2025Q4"


async def test_get_earnings_transcript_whitespace_quarter_stripped() -> None:
    """quarter 前後帶空白 → strip 後再 upper,打到正確季別(不帶空白)。"""
    with respx.mock(base_url="http://test-api") as mock:
        mock.get("/api/companies/AAPL/transcripts").respond(
            200,
            json=[
                {"quarter": "2025Q4", "segments": 71, "fetched_at": "2026-02-01T12:00:00Z"},
                {"quarter": "2025Q3", "segments": 68, "fetched_at": "2025-11-01T12:00:00Z"},
            ],
        )
        detail_route = mock.get("/api/companies/AAPL/transcripts/2025Q3").respond(
            200,
            json={"ticker": "AAPL", "quarter": "2025Q3", "segments_total": 68,
                  "fetched_at": "2025-11-01T12:00:00Z", "segments": []},
        )
        result = await _unwrap(tools.get_earnings_transcript)(
            ticker="aapl", quarter="  2025q3  "
        )

    assert detail_route.called
    assert result["quarter"] == "2025Q3"


# ---- 回傳值斷言:本輪改到的 tool ------------------------------------------
#
# 這個檔原本 80 個 tool 測試只驗 URL / params,不驗回傳值 —— 也就是「tool 把後端 payload
# 弄壞了」這類 bug 完全測不到。這輪不重寫整批,只補上本次動過的那幾個 tool。


_FINANCIAL_ROWS = [
    {
        "ticker": "SHOP",
        "fiscal_year": 2025,
        "fiscal_period": "FY",
        "period_end": "2025-12-31",
        "reporting_currency": "CAD",  # 非 USD 的 filer 確實存在(ADR / 外國申報人)
        "revenue": 1000.0,
    },
    {
        "ticker": "SHOP",
        "fiscal_year": 2025,
        "fiscal_period": "Q3",
        "period_end": "2025-09-30",
        "reporting_currency": "CAD",
        "revenue": 250.0,
    },
]


@pytest.mark.parametrize(
    ("tool_fn", "path"),
    [
        (tools.get_income_statements, "/api/financials/income"),
        (tools.get_balance_sheets, "/api/financials/balance"),
        (tools.get_cash_flow_statements, "/api/financials/cashflow"),
    ],
)
async def test_financial_statement_tools_return_backend_rows_verbatim(tool_fn, path: str) -> None:
    """三張報表 tool 原樣回後端列 —— 特別是 reporting_currency 不可被吃掉。

    平台不做 FX 換算,agent 只能靠每列的 reporting_currency 判斷口徑;
    這個欄位一旦在轉手時被丟掉,下游就會拿 USD 價格去跟 CAD 財報算 P/E。
    """
    with respx.mock(base_url="http://test-api") as mock:
        mock.get(path).respond(200, json=_FINANCIAL_ROWS)
        result = await _unwrap(tool_fn)(ticker="shop")

    assert result == _FINANCIAL_ROWS
    assert result[0]["reporting_currency"] == "CAD"


async def test_get_latest_period_returns_backend_payload() -> None:
    """三表合體原樣回傳(含各子物件的 reporting_currency)。"""
    payload = {
        "ticker": "SHOP",
        "period_end": "2025-12-31",
        "income": {"reporting_currency": "CAD", "revenue": 1000.0},
        "balance": {"reporting_currency": "CAD", "total_assets": 5000.0},
        "cash_flow": {"reporting_currency": "CAD", "operating_cash_flow": 300.0},
    }
    with respx.mock(base_url="http://test-api") as mock:
        mock.get("/api/financials/latest").respond(200, json=payload)
        result = await _unwrap(tools.get_latest_period)(ticker="shop")

    assert result == payload


async def test_financial_tools_forward_q4_and_get_empty() -> None:
    """DB 裡沒有 Q4 列:後端收下 period 但回空 —— tool 誠實把空 list 傳回去,不假裝有資料。"""
    with respx.mock(base_url="http://test-api") as mock:
        route = mock.get("/api/financials/income").respond(200, json=[])
        result = await _unwrap(tools.get_income_statements)(
            ticker="aapl", period="quarterly"
        )

    assert dict(route.calls.last.request.url.params)["period"] == "quarterly"
    assert result == []


async def test_get_market_news_returns_backend_payload_with_topic_codes() -> None:
    """market news 原樣回傳;topics[].topic 是全小寫底線代碼(非 Title Case 顯示標籤)。"""
    payload = [
        {
            "title": "Fed holds rates",
            "url": "https://example.com/a",
            "source": "Example",
            "published_at": "2026-08-20T12:00:00Z",
            "topics": [
                {"topic": "economy_monetary", "relevance": 0.9},
                {"topic": "financial_markets", "relevance": 0.7},
            ],
        }
    ]
    with respx.mock(base_url="http://test-api") as mock:
        route = mock.get("/api/market/news").respond(200, json=payload)
        result = await _unwrap(tools.get_market_news)(topic="economy_monetary", limit=5)

    assert dict(route.calls.last.request.url.params) == {
        "limit": "5",
        "topic": "economy_monetary",
    }
    assert result == payload


async def test_get_market_news_unknown_topic_returns_empty_list() -> None:
    """topic 對不上是**靜默回空 list**,不是報錯 —— tool 必須原樣把空 list 傳回去。"""
    with respx.mock(base_url="http://test-api") as mock:
        mock.get("/api/market/news").respond(200, json=[])
        result = await _unwrap(tools.get_market_news)(topic="Financial Markets")

    assert result == []


# ---- tool description 契約回歸:釘住依 prod 實測修正的說法 -------------------
#
# tool description 是 LLM 唯一的語意來源,講錯就是安靜地算錯(拿 USD 價格對 CAD 財報算
# P/E、以為日 K 是還原價、拿 Title Case 去篩 topic 篩到空)。這裡把已推翻的說法釘成黑名單。
#
# **關鍵:斷言的是 FastMCP 實際送給 LLM 的 description,不是 `__doc__`。**
# FastMCP 只把 `Args:` **之前**的 prose 當 tool description,`Args:` 的內容拆進 input
# schema 的 param description,而 `Returns:` 那段**整段不會出現在 wire 上**。也就是說
# 「寫在 docstring 裡」不等於「LLM 讀得到」—— 警告若擺在 Args/Returns 之後就是隱形的。
# 用 `__doc__` 斷言會通過但保護不到真正的契約,所以這裡走 `to_mcp_tool()`。


_FINANCIAL_TOOLS = (
    tools.get_income_statements,
    tools.get_balance_sheets,
    tools.get_cash_flow_statements,
    tools.get_latest_period,
)


async def _mcp_tool(tool_fn):
    """拿到 FastMCP 註冊後、真正會序列化上 wire 的那個 tool 物件。

    `@mcp.tool` 在這個 FastMCP 版本回的是原函式,description / inputSchema 要從
    provider 的註冊表查(跟 `test_all_tools_registered` 同一條路)。
    """
    name = _unwrap(tool_fn).__name__
    registered = {t.name: t for t in await mcp._local_provider.list_tools()}
    return registered[name].to_mcp_tool()


async def _doc(tool_fn) -> str:
    """FastMCP 實際送給 LLM 的 tool description(不是 raw docstring)。"""
    return (await _mcp_tool(tool_fn)).description or ""


async def _param_doc(tool_fn, param: str) -> str:
    """FastMCP 送給 LLM 的某個參數說明(來自 docstring 的 Args: 區塊)。"""
    schema = (await _mcp_tool(tool_fn)).inputSchema
    return schema["properties"][param].get("description", "")


async def test_returns_section_is_not_sent_to_llm() -> None:
    """釘住上面那個假設本身:`Returns:` 不會進 wire description。

    這條測試存在是為了讓「警告要放 Args: 之前」這個規則有據可考 —— 哪天 FastMCP 改成
    連 Returns 一起送,這裡會紅,提醒可以把說明搬回去。
    """
    doc = await _doc(tools.get_income_statements)
    assert "Returns:" not in doc
    assert "Args:" not in doc


@pytest.mark.parametrize("tool_fn", _FINANCIAL_TOOLS, ids=lambda t: _unwrap(t).__name__)
async def test_financial_descriptions_drop_usd_normalized_claim(tool_fn) -> None:
    """財報 tool 不得再宣稱 USD-normalized —— ETL 完全不換匯,存的是申報原幣。"""
    doc = await _doc(tool_fn)
    assert "USD-normalized" not in doc
    assert "reporting_currency" in doc
    assert "NO FX conversion" in doc


@pytest.mark.parametrize("tool_fn", _FINANCIAL_TOOLS, ids=lambda t: _unwrap(t).__name__)
async def test_financial_descriptions_warn_about_cross_currency_ratios(tool_fn) -> None:
    """必須明講:reporting_currency != 'USD' 時不要拿 USD 價格算 P/E 之類的比率。"""
    doc = await _doc(tool_fn)
    assert "reporting_currency != 'USD'" in doc
    assert "P/E" in doc


@pytest.mark.parametrize("tool_fn", _FINANCIAL_TOOLS, ids=lambda t: _unwrap(t).__name__)
async def test_financial_period_param_states_q4_does_not_exist(tool_fn) -> None:
    """`period` 參數說明必須明講 DB 裡根本沒有 Q4 列(傳 "Q4" 後端會收但永遠回空)。"""
    doc = await _param_doc(tool_fn, "period")
    assert "Q4" in doc
    assert "沒有" in doc


@pytest.mark.parametrize(
    "tool_fn", (tools.list_daily_prices, tools.get_latest_price), ids=lambda t: _unwrap(t).__name__
)
async def test_price_descriptions_say_as_traded_not_adjusted(tool_fn) -> None:
    """價格 cadence 行不得自稱 adjusted —— 此端點回的是未還原的 as-traded 價。"""
    doc = await _doc(tool_fn)
    assert "as-traded (NOT split/dividend adjusted)" in doc
    assert "; adjusted;" not in doc
    assert "adj_close" in doc  # 要指路:底層表有這欄,得走 execute_readonly_sql


async def test_market_news_topic_param_lists_real_lowercase_topics() -> None:
    """topic 合法值必須是 prod 實測的 15 個小寫底線代碼,不是 Title Case 顯示標籤。"""
    doc = await _param_doc(tools.get_market_news, "topic")
    for code in (
        "financial_markets",
        "earnings",
        "finance",
        "technology",
        "life_sciences",
        "energy_transportation",
        "retail_wholesale",
        "economy_macro",
        "manufacturing",
        "real_estate",
        "mergers_and_acquisitions",
        "economy_fiscal",
        "ipo",
        "economy_monetary",
        "blockchain",
    ):
        assert code in doc, f"get_market_news topic param missing code {code!r}"
    for refuted in ("Financial Markets", "Mergers & Acquisitions", "Economy - Macro/Overall"):
        assert refuted not in doc, f"topic param still carries refuted label {refuted!r}"
