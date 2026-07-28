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


# ---- prices:不帶 start/end 時自動回推 start(拿最近 N 筆,非最舊 N 筆)-----------
#
# 後端 ORDER BY date/dt ASC + LIMIT,只給 limit 會回最舊 N 筆;工具在無邊界時自動回推
# start,把視窗收斂到約 N 個交易日,使「預設回最近 N 筆」成立。start 含動態日期,故不放
# 進上面的精確比對 _CASES,獨立驗證。

from datetime import date, datetime, timedelta, timezone  # noqa: E402
from math import ceil  # noqa: E402


async def test_list_daily_prices_autoderives_start_when_no_bounds() -> None:
    """list_daily_prices 不帶 start/end:自動送出回推的 start、不送 end。"""
    with respx.mock(base_url="http://test-api") as mock:
        route = mock.get("/api/prices/daily").respond(200, json=[])
        await _unwrap(tools.list_daily_prices)(ticker="aapl", limit=30)

    params = dict(route.calls.last.request.url.params)
    assert params["ticker"] == "AAPL"
    assert params["limit"] == "30"
    assert "end" not in params  # 上界留白 = 取到最新
    assert "start" in params
    derived = date.fromisoformat(params["start"])
    expected = date.today() - timedelta(days=ceil(30 * 1.4))
    assert abs((derived - expected).days) <= 1  # 容忍 assert 期間跨過午夜


async def test_list_daily_prices_keeps_explicit_bounds() -> None:
    """顯式帶 start/end 時不回推,原樣轉發。"""
    with respx.mock(base_url="http://test-api") as mock:
        route = mock.get("/api/prices/daily").respond(200, json=[])
        await _unwrap(tools.list_daily_prices)(
            ticker="aapl", start="2024-01-01", end="2024-06-30", limit=30
        )

    params = dict(route.calls.last.request.url.params)
    assert params["start"] == "2024-01-01"
    assert params["end"] == "2024-06-30"


async def test_list_hourly_prices_autoderives_start_when_no_bounds() -> None:
    """list_hourly_prices 不帶 start/end:自動送出回推的 start(UTC ISO)、不送 end。"""
    with respx.mock(base_url="http://test-api") as mock:
        route = mock.get("/api/prices/hourly").respond(200, json=[])
        await _unwrap(tools.list_hourly_prices)(ticker="aapl", limit=100)

    params = dict(route.calls.last.request.url.params)
    assert params["ticker"] == "AAPL"
    assert params["limit"] == "100"
    assert "end" not in params
    assert "start" in params
    derived = datetime.strptime(params["start"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    expected = datetime.now(timezone.utc) - timedelta(days=ceil(100 / 5))
    assert abs((derived - expected).total_seconds()) <= 172800  # 2 天內(容忍執行耗時)


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
