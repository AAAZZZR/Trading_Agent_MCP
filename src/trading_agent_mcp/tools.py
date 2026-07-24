"""@mcp.tool —— 對應 Trading_Agent REST endpoint 的市場資料 tool。

大多數 per-domain tool(`list_companies` / `get_income_statements` 等)內部就是一行
`await api.get(path, params=...)`,LLM 端透過 docstring 理解語意。tool 名稱刻意動詞開頭
(`list_` = 多筆 / `get_` = 單筆 / `search_` = 模糊查詢 / `describe_` = schema 自省),
讓 agent 一眼就能挑對工具。

自由查詢工具 `execute_readonly_sql` 給 power user / AI agent 跑任意 SELECT,走獨立
readonly Postgres 連線(`db.py`),經 sqlparse + readonly role + statement timeout
三層防護;搭配 `describe_table` 讓 agent 先自省 schema 再下 SQL。

回傳型別目前以 dict / list[dict] 起步(求覆蓋度);所有金額單位為 USD,日期為
YYYY-MM-DD ISO 字串,除非個別 docstring 另有說明。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from math import ceil
from typing import Any, Literal

from fastmcp.exceptions import ToolError
from fastmcp.server.auth import require_scopes

from trading_agent_mcp.api_client import api
from trading_agent_mcp.db import ReadonlyDBNotConfigured, get_pool
from trading_agent_mcp.server import mcp
from trading_agent_mcp.sql_query import (
    SQLValidationError,
    clamp_limit,
    truncate_payload,
    validate_select_only,
)

# 全站唯讀資料服務:每個 tool 都只查詢、不改動任何狀態;openWorldHint=False = 只觸及
# 自家 DB,不與外部世界互動。新增 tool 一律要帶上(有註冊性測試強制)。
_READONLY_ANNOTATIONS = {"readOnlyHint": True, "openWorldHint": False}

# ============================================================
# Onboarding —— 新 agent 的入口(刻意放最前面,tools/list 第一個就看到)
# ============================================================


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def start_here() -> dict[str, Any]:
    """START HERE — the orientation index for any agent new to investor-db. Call this first.

    Most MCP clients inspect tools/list before anything else and never read the server
    instructions, prompts, or resources — so a new agent can miss that this product already
    ships ready-made report prompts and a data dictionary. This tool is the map: it returns
    the product scope and limits, how to pick the right tool, the end-to-end workflows
    (including the prompts/resources you would otherwise overlook), and the honesty rules for
    quoting figures.

    This is the capabilities / getting-started / what-can-this-do entry point. It takes no
    arguments, calls no backend, and is always safe and free to call. After reading it, call
    get_data_coverage before quoting any exact number.

    Returns:
        dict {product, scope, first_call, how_to_pick_a_tool, workflows, prompts, resources,
        honesty_rules} — a compact orientation, not live data.
    """
    return {
        "product": "investor-db",
        "scope": (
            "US equities + ADRs only. EOD or slower — never real-time. ~3-5 years of history. "
            "No analyst ratings, revenue forecasts, or estimate revisions (only two vendor "
            "fields exist: earnings estimate EPS and an analyst target price)."
        ),
        "first_call": (
            "Call get_data_coverage before quoting any exact figure — it reports per-domain "
            "freshness and coverage and is the trust anchor for every other tool."
        ),
        "how_to_pick_a_tool": [
            "Don't know the exact ticker -> search_companies",
            "Want a fast four-lens traffic-light conclusion -> get_analysis",
            "Want the raw data bundle to analyze yourself -> get_objective_report",
            "Need positioning -> get_short_interest for open positions; get_short_volume for separate off-exchange flow",
            "Need any market number -> fetch it with a tool; never answer from memory (it is stale)",
            "Flexible or statistical queries (pro tier) -> describe_table, then execute_readonly_sql",
        ],
        "workflows": [
            {
                "name": "quick_answer_in_chat",
                "best_for": "a fast conclusion inside the conversation",
                "steps": ["get_company (or search_companies)", "get_analysis", "get_objective_report"],
            },
            {
                "name": "full_html_research_report",
                "best_for": "a polished, self-contained HTML equity report artifact",
                "use_prompt": "build_stock_report",
                "read_resources": ["data://dictionary", "data://report-template"],
            },
            {
                "name": "company_profile",
                "best_for": "what does this company actually do (business overview)",
                "use_prompt": "company_profile",
            },
            {
                "name": "compare_two_names",
                "best_for": "a side-by-side comparison of two tickers",
                "use_prompt": "compare_stocks",
            },
        ],
        "prompts": [
            "analyze_stock",
            "analyze_stock_full",
            "compare_stocks",
            "build_stock_report",
            "company_profile",
        ],
        "resources": {
            "data://dictionary": "Field-level semantics, units, codes, and lag rules. Read before interpreting numbers.",
            "data://analysis-playbook": "Step-by-step analysis methodology (document form of analyze_stock).",
            "data://report-template": "The HTML report design system used by build_stock_report.",
        },
        "honesty_rules": [
            "Every figure needs an as-of date (from get_data_coverage or the row itself).",
            "null means missing, never zero.",
            "Do not invent price targets or forecasts.",
            "13F institutional holdings carry a ~45-day filing lag.",
            "Never substitute daily short-sale volume for twice-monthly open short interest.",
            "News is a vendor aggregate overlay, not first-party SEC evidence — keep it separate.",
        ],
    }


# ============================================================
# Meta / data coverage(資料新鮮度與覆蓋範圍 —— agent 信任基石)
# ============================================================


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def get_data_coverage() -> dict[str, Any]:
    """Report what this dataset actually covers and how fresh each domain is.

    Call this BEFORE quoting exact numbers to the user, whenever data looks stale or
    surprising, and whenever asked "how current is this data?". It is the trust anchor for
    every other tool — use it to decide whether a figure is current enough to rely on.

    No tier requirement: every caller can read coverage.

    Reading the fields:
        - `as_of`: when this coverage report itself was generated.
        - `domains[]`: one entry per data domain (prices_daily, financials, insider, 13f,
          options, macro, …).
        - `domains[].last_data_point`: the newest DATE in the data itself (e.g. the most
          recent trading day on file). This is what "how new is the data" means — NOT the
          ingest timestamp.
        - `domains[].last_ingest_ok`: when the last successful refresh ran (pipeline health).
        - `domains[].coverage.tickers`: how many symbols the domain covers.
        - `domains[].date_range`: {from, to} span of available history.
        - `domains[].cadence`: how often the domain refreshes.
        - `domains[].known_gaps`: free-text caveat. A null here means "no known gap recorded",
          which is NOT the same as "zero gaps" — absence of a note is not a guarantee.

    Coverage is US equities + ADRs only: no indices (as tradable tickers), no crypto, no
    non-US listings, EOD-or-slower. Analyst data is limited to two vendor fields
    (earnings_calendar.estimate_eps, overview analyst_target_price) — no ratings or
    revenue forecasts.

    Data cadence: this report refreshed daily; per-domain freshness is in each domain entry.

    Returns:
        dict {as_of, notes, domains: [...]}. See field notes above.
    """
    return await api.get("/api/meta/coverage")


# ============================================================
# Companies
# ============================================================


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def list_companies(limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
    """列出被追蹤的美股公司,依 ticker 字母排序。

    這是「瀏覽 / 抽樣」用,**MCP 端預設只回 200 筆**防灌爆 context。要找特定公司請改用
    `search_companies`(吃名稱 / 模糊 ticker);要跑全量統計或自訂篩選請用
    `execute_readonly_sql` 查 `companies` 表。需要更多筆數時調大 limit 或用 offset 分頁。

    Data cadence: company registry refreshed daily ~06:00 UTC.

    Args:
        limit: 最多回傳幾筆(預設 200)。
        offset: 跳過前 N 筆,分頁用(>=0,預設 0)。

    Returns:
        list[dict],每筆欄位:ticker, cik, name, sector, sic_code, industry,
        exchange, country(目前恆為 null), is_active(bool), first_seen(YYYY-MM-DD),
        last_updated(ISO datetime)。
    """
    return await api.get("/api/companies", params={"limit": limit, "offset": offset})


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def search_companies(q: str, limit: int = 20) -> list[dict[str, Any]]:
    """以 ticker 或公司名稱關鍵字模糊搜尋公司(typeahead 用),適合「我不知道精確 ticker」時。

    比對 ticker 前綴 / 子字串與名稱子字串(皆 case-insensitive),排序:ticker 完全相等 →
    ticker 前綴 → name 前綴 → 其餘子字串命中,同級短 ticker 優先。找到精確 ticker 後可再用
    `get_company` 取完整基本資料。

    Data cadence: company registry refreshed daily ~06:00 UTC.

    Args:
        q: 搜尋關鍵字,例如 "apple"、"nvda"、"semiconductor"(至少 1 個字元)。
        limit: 最多回傳幾筆(1-50,預設 20)。

    Returns:
        list[dict],精簡欄位:ticker, name, exchange。無命中回空 list。
    """
    return await api.get("/api/companies/search", params={"q": q, "limit": limit})


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def get_company(ticker: str) -> dict[str, Any]:
    """以精確 ticker 取得單一公司的完整基本資料。不知道精確 ticker 時先用 `search_companies`。

    Data cadence: company registry refreshed daily ~06:00 UTC.

    Args:
        ticker: 美股代號,例如 AAPL、MSFT、NVDA(大小寫不拘,自動轉大寫)。

    Returns:
        dict,欄位:ticker, cik, name, sector, sic_code, industry, exchange,
        country(目前恆為 null), is_active(bool), status('active'|'delisted'),
        delisted_at(下市日 YYYY-MM-DD,active 時為 null), first_seen(YYYY-MM-DD),
        last_updated(ISO datetime)。**判斷是否下市以 status / delisted_at 為準
        (價格停更 ≠ 下市)**。查無此 ticker → tool error(後端 404)。
    """
    return await api.get(f"/api/companies/{ticker.upper()}")


# ============================================================
# Filings
# ============================================================


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def list_filings(
    ticker: str,
    form_type: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """列出某公司提交過的 SEC filings,依 filed_at 由新到舊。

    Data cadence: discovered daily 06:00 UTC, parsed same day.

    Args:
        ticker: 美股代號(自動轉大寫)。
        form_type: 表格類型過濾,例如 "10-K"(年報)、"10-Q"(季報)、"8-K"(臨時公告)、"4"(內部人交易);省略 = 全部。
        since: 起始 filed_at(YYYY-MM-DD),包含。
        until: 結束 filed_at(YYYY-MM-DD),包含。
        limit: 最多回傳幾筆(1-500,預設 50)。

    Returns:
        list[dict],每筆含 accession(SEC accession number)、form_type、filed_at
        等後設資料。要讀內文先用 `list_filing_sections` 找章節,再用 `get_filing_section`。
    """
    params: dict[str, Any] = {"ticker": ticker.upper(), "limit": limit}
    if form_type is not None:
        params["form_type"] = form_type
    if since is not None:
        params["since"] = since
    if until is not None:
        params["until"] = until
    return await api.get("/api/filings", params=params)


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def get_filing(accession: str) -> dict[str, Any]:
    """以 SEC accession number 取得單一 filing 的後設資料。

    Data cadence: discovered daily 06:00 UTC, parsed same day.

    Args:
        accession: SEC accession,例如 "0000320193-24-000123"。
    """
    return await api.get(f"/api/filings/{accession}")


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def list_filing_sections(accession: str) -> list[dict[str, Any]]:
    """列出某 filing 已解析的章節目錄(item_code / 標題 / 字元範圍,不含內文)。

    Data cadence: discovered daily 06:00 UTC, parsed same day.

    Args:
        accession: SEC accession。
    """
    return await api.get(f"/api/filings/{accession}/sections")


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def get_filing_section(accession: str, item_code: str) -> dict[str, Any]:
    """取得單一 filing 章節的完整內文。**先呼叫 `list_filing_sections(accession)` 拿正確的
    item_code,不要憑常識猜**(照 "Item 1A" 這種寫法會 404)。

    item_code 是本平台解析時的內部代碼,**不是** SEC 表單上的 "Item 1A" 字面。實際格式:
      - 10-K / 10-Q:羅馬數字 Part + 項次,例 "I.1A"(Part I Item 1A 風險因素)、
        "II.7"(Part II Item 7 MD&A)。
      - 8-K:數字 item 代碼,例 "2.02"(財報結果)、"9.01"(財報附件)。
    唯一可靠的做法是先用 `list_filing_sections` 列出該 filing 真正存在的 item_code 再帶進來。

    Data cadence: discovered daily 06:00 UTC, parsed same day.

    Args:
        accession: SEC accession。
        item_code: 章節代碼,**取自 `list_filing_sections` 回傳的 item_code**
            (例 "I.1A"、"II.7"、"2.02"、"9.01");原樣帶入,不要自行改寫成 "Item X"。
    """
    return await api.get(f"/api/filings/{accession}/sections/{item_code}")


# ============================================================
# Financials
# ============================================================


# period 篩選:後端把 "annual" → fiscal_period='FY'、"quarterly" → IN ('Q1','Q2','Q3')。
# **注意 quarterly 刻意不含 Q4** —— 多數 filer 不單獨申報 Q4,其數字隱含在全年 FY 裡
# (Q4 ≈ FY − Q1 − Q2 − Q3),所以要 Q4 請取 annual(FY)那期。回傳列本身帶
# fiscal_year / fiscal_period 欄位,agent 可據此辨識是哪一期。
Period = Literal["annual", "quarterly"]


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def get_income_statements(
    ticker: str,
    period: Period | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """取得損益表(營收、毛利、營業利益、淨利、EPS 等,金額 USD),依期末由新到舊。

    Data cadence: from SEC XBRL filings, refreshed daily 07:00 UTC for recent filers; USD-normalized.

    Args:
        ticker: 美股代號(自動轉大寫)。
        period: "annual"(年報 FY)或 "quarterly"(季報,**僅 Q1-Q3**;Q4 隱含在 FY 裡,
            要 Q4 請用 annual);省略 = 兩者都回。
        limit: 最多回傳幾筆(1-200,預設 20)。

    Returns:
        list[dict],每筆含 fiscal_year、fiscal_period、period_end(YYYY-MM-DD)與各項
        財務數字(USD)。要一次拿三表合體用 `get_latest_period`。
    """
    params: dict[str, Any] = {"ticker": ticker.upper(), "limit": limit}
    if period is not None:
        params["period"] = period
    return await api.get("/api/financials/income", params=params)


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def get_balance_sheets(
    ticker: str,
    period: Period | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """取得資產負債表(現金、應收、存貨、PPE、總資產、總負債、權益等,金額 USD),依期末由新到舊。

    Data cadence: from SEC XBRL filings, refreshed daily 07:00 UTC for recent filers; USD-normalized.

    Args:
        ticker: 美股代號(自動轉大寫)。
        period: "annual"(FY)或 "quarterly"(**僅 Q1-Q3**;Q4 隱含在 FY,要 Q4 用 annual);
            省略 = 兩者都回。
        limit: 最多回傳幾筆(1-200,預設 20)。

    Returns:
        list[dict],每筆含 fiscal_year、fiscal_period、period_end(YYYY-MM-DD)與各項
        資產負債科目(USD)。
    """
    params: dict[str, Any] = {"ticker": ticker.upper(), "limit": limit}
    if period is not None:
        params["period"] = period
    return await api.get("/api/financials/balance", params=params)


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def get_cash_flow_statements(
    ticker: str,
    period: Period | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """取得現金流量表(營業現金流、capex、自由現金流、股利、回購等,金額 USD),依期末由新到舊。

    Data cadence: from SEC XBRL filings, refreshed daily 07:00 UTC for recent filers; USD-normalized.

    Args:
        ticker: 美股代號(自動轉大寫)。
        period: "annual"(FY)或 "quarterly"(**僅 Q1-Q3**;Q4 隱含在 FY,要 Q4 用 annual);
            省略 = 兩者都回。
        limit: 最多回傳幾筆(1-200,預設 20)。

    Returns:
        list[dict],每筆含 fiscal_year、fiscal_period、period_end(YYYY-MM-DD)與各項
        現金流科目(USD)。
    """
    params: dict[str, Any] = {"ticker": ticker.upper(), "limit": limit}
    if period is not None:
        params["period"] = period
    return await api.get("/api/financials/cashflow", params=params)


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def get_latest_period(
    ticker: str,
    period: Period | None = None,
) -> dict[str, Any]:
    """取得最近一期的三張財報合體(income + balance + cash_flow,同一個 period_end,金額 USD)。

    Data cadence: from SEC XBRL filings, refreshed daily 07:00 UTC for recent filers; USD-normalized.

    Args:
        ticker: 美股代號(自動轉大寫)。
        period: "annual" 取最近一個年報期(FY);"quarterly" 取最近一季(**僅 Q1-Q3**,
            Q4 隱含在 FY);省略 = 不限期別取最新。

    Returns:
        dict,含 period_end(YYYY-MM-DD)與 income / balance / cash_flow 三個子物件。
        查無財報資料 → tool error(後端 404)。
    """
    params: dict[str, Any] = {"ticker": ticker.upper()}
    if period is not None:
        params["period"] = period
    return await api.get("/api/financials/latest", params=params)


# ============================================================
# Insider trades(Form 4)
# ============================================================


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def list_insider_trades(
    ticker: str,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """列出單一公司內部人(高管 / 董事 / 大股東)依 SEC Form 4 申報的交易紀錄,依交易日新到舊。

    這是「單 ticker」視角。要跨整個 universe 篩 open-market 買進(例如「最近誰在買」),
    改用 `screen_insider_buys`。

    Data cadence: Form 4; SEC requires filing within 2 business days of the trade; ingested daily.

    Args:
        ticker: 美股代號(自動轉大寫)。
        since: 起始 transaction_date(YYYY-MM-DD),包含。
        until: 結束 transaction_date(YYYY-MM-DD),包含。
        limit: 最多回傳幾筆(1-1000,預設 100)。

    Returns:
        list[dict],每筆含 transaction_date、insider_name、insider_title、
        transaction_code(P=買 / S=賣 等)、shares、price_per_share(USD)等欄位。
    """
    params: dict[str, Any] = {"ticker": ticker.upper(), "limit": limit}
    if since is not None:
        params["since"] = since
    if until is not None:
        params["until"] = until
    return await api.get("/api/insider", params=params)


# ============================================================
# Prices
# ============================================================


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def list_daily_prices(
    ticker: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    """取得第一手每日 OHLC + 成交量(價格 USD),依日期升冪(最舊在前)。最多約 5 年歷史。

    這是畫長線圖 / 回測 / 算報酬率該用的日 K(直存第一手日線)。涵蓋最多約 5 年,
    依上市時間而異(新上市股較短)。**不含還原價(adj_close)** —— 跨除權息 / 分割
    要自行用 `list_dividends` / `list_splits` 調整。要 intraday(小時)粒度改用
    `list_hourly_prices`。

    **拿「最近」資料的正確做法**:後端一律 date 升冪(最舊在前)再套 limit,所以
    「縮小 limit」拿到的是**最舊**的 N 筆,不是最新的。本工具在你**不帶 start/end**
    時會自動回推一個起始日,讓預設就回**最近約 N 筆**(N=limit);要精確區間才自己帶
    start/end。

    Data cadence: EOD T+1, refreshed Mon-Sat ~10:00 UTC; adjusted; 5-year rolling window; NOT real-time.

    Args:
        ticker: 美股代號(自動轉大寫)。
        start: 起始日期(YYYY-MM-DD),包含。省略且 end 也省略 = 自動回推,回最近約 N 筆。
        end: 結束日期(YYYY-MM-DD),包含。省略 = 不設上界(取到最新)。
        limit: 最多幾筆(1-10000,預設 2000)。省略 start/end 時同時決定回溯視窗大小。

    Returns:
        list[dict],每筆含 ticker、date(YYYY-MM-DD)、open、high、low、close(USD)、
        volume,date 升冪。**不含 adj_close**。只要最新一筆用 `get_latest_price`。
        查無資料回空 list。
    """
    params: dict[str, Any] = {"ticker": ticker.upper(), "limit": limit}
    # 後端是 ORDER BY date ASC + LIMIT,只給 limit 會拿到最舊 N 筆。使用者沒帶任何邊界時
    # 自動回推起始日,把視窗收斂到約 limit 個交易日,讓預設回「最近約 N 筆」。回推倍率取
    # 1.4(週末+假日 → 每日曆日約 0.69 交易日,1.4 略低於精確界 1.449,寧可少收幾根也
    # 不要因視窗過大而讓 ASC+LIMIT 砍掉最新的幾根)。
    if start is None and end is None:
        start = (date.today() - timedelta(days=ceil(limit * 1.4))).isoformat()
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    return await api.get("/api/prices/daily", params=params)


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def get_latest_price(ticker: str) -> dict[str, Any]:
    """取得最新一個交易日的第一手日 K(OHLC + 成交量,價格 USD)。

    跟 `list_daily_prices` 同一個第一手日線來源(取最後一筆)。

    Data cadence: EOD T+1, refreshed Mon-Sat ~10:00 UTC; adjusted; 5-year rolling window; NOT real-time.

    Args:
        ticker: 美股代號(自動轉大寫)。

    Returns:
        dict,含 ticker、date(YYYY-MM-DD)、open、high、low、close(USD)、volume。
        **不含 adj_close**。查無價格 → tool error(後端 404)。
    """
    return await api.get("/api/prices/daily/latest", params={"ticker": ticker.upper()})


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def list_hourly_prices(
    ticker: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    """取得每小時 OHLC + 成交量(dt 升冪,timestamptz)。

    粒度比 `list_daily_prices` 細,適合做 intraday 分析或 backtest 對齊。
    歷史深度有限(60 天滾動保留),要長區間請改用 `list_daily_prices`。

    **拿「最近」資料的正確做法**:跟日 K 一樣,後端是 dt 升冪 + limit,只給 limit 拿到的是
    **最舊**的 N 筆。本工具在你**不帶 start/end**時會自動回推一個起始時間,讓預設回**最近約
    N 根**小時 K;要精確區間才自己帶 start/end。

    Data cadence: 60-minute bars, Mon-Fri ~22:00 UTC refresh; 60-day rolling retention.

    Args:
        ticker: 美股代號。
        start: 起始 UTC datetime(ISO,例 "2026-05-22T13:30:00Z"),包含。省略且 end 也省略 =
            自動回推,回最近約 N 根。
        end: 結束 UTC datetime,包含。省略 = 不設上界(取到最新)。
        limit: 最多幾筆(1-20000,預設 5000)。省略 start/end 時同時決定回溯視窗大小。
    """
    params: dict[str, Any] = {"ticker": ticker.upper(), "limit": limit}
    # 同 list_daily_prices:ORDER BY dt ASC + LIMIT。沒帶邊界時自動回推起始時間,把視窗
    # 收斂到約 limit 根小時 K(美股每交易日約 7 根 → limit/7 個交易日 → 再乘 1.4 換成日曆
    # 天,即 limit/5 天),讓預設回「最近約 N 根」。
    if start is None and end is None:
        start = (
            datetime.now(timezone.utc) - timedelta(days=ceil(limit / 5))
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    return await api.get("/api/prices/hourly", params=params)


# ============================================================
# Holdings(第一手自算:13F + Form 4 + 流通股數)
# ============================================================


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def list_institutional_holders(ticker: str) -> list[dict[str, Any]]:
    """取得主要機構持股清單(前幾大,第一手 SEC 13F 自算,非第三方聚合)。

    這是「前幾大」摘要視圖。要完整名單、可分析季度增減變動的 13F 原始資料,
    改用 `list_13f_holders`(by stock)/ `list_13f_portfolio`(by filer)。

    Data cadence: quarterly holdings with 45-day SEC filing lag; refreshed on a daily 1/7 filer rotation.

    Args:
        ticker: 美股代號(自動轉大寫)。

    Returns:
        list[dict],每筆含 holder、date_reported、shares、value(USD)、
        pct_held、pct_change。
    """
    return await api.get("/api/holdings/institutions", params={"ticker": ticker.upper()})


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def get_holders_breakdown(ticker: str) -> dict[str, Any]:
    """取得內部人 / 機構持股比例總覽(第一手自算:13F 機構持倉 + Form 4 內部人 + 流通股數)。

    Data cadence: quarterly holdings with 45-day SEC filing lag; refreshed on a daily 1/7 filer rotation.

    Args:
        ticker: 美股代號(自動轉大寫)。

    Returns:
        dict,含 insiders_pct、institutions_pct、institutions_float_pct、
        institutions_count、source。比例為 0-1 的小數。
    """
    return await api.get("/api/holdings/major", params={"ticker": ticker.upper()})


# ============================================================
# 13F-HR(第一手 SEC 機構持股原始資料 —— 完整名單與季度變動)
# ============================================================


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def list_13f_holders(
    ticker: str,
    quarter_end: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """列出某股票的所有機構持有人(第一手 SEC 13F-HR),依持有市值由大到小。

    比 `list_institutional_holders`(Yahoo 前 10 大)完整:涵蓋全部申報機構、季度精度、
    並帶較上季的持股變動。要看某一機構的整個組合用 `list_13f_portfolio`。

    Data cadence: quarterly holdings with 45-day SEC filing lag; refreshed on a daily 1/7 filer rotation.

    Args:
        ticker: 美股代號(自動轉大寫)。
        quarter_end: 季底(YYYY-MM-DD,例 "2024-12-31");省略 = 自動取該股票最新一季。
        limit: 最多回傳幾筆(1-1000,預設 100)。**JSON 模式後端硬上界 1000**,傳更大值會被
            靜默截到 1000。

    Returns:
        list[dict],每筆含 filer_cik、filer_name、ticker、cusip、quarter_end(YYYY-MM-DD)、
        shares、market_value(USD)、change_in_shares、change_type。change_type 值域:
        "new"(本季新進)、"increase"(加碼)、"decrease"(減碼)、"sold_all"(清倉,
        shares 為 0/NULL)、"no_change"(持平)。
    """
    params: dict[str, Any] = {"ticker": ticker.upper(), "limit": limit}
    if quarter_end is not None:
        params["quarter_end"] = quarter_end
    return await api.get("/api/13f/holders", params=params)


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def list_13f_portfolio(
    cik: str,
    quarter_end: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """列出某機構(以 CIK 指定)整個 13F 持倉組合,依持有市值由大到小。

    用來回答「Berkshire / 某基金這季持有哪些股票、各多少」。CIK 可從 `list_13f_holders`
    回傳的 filer_cik 取得。

    Data cadence: quarterly holdings with 45-day SEC filing lag; refreshed on a daily 1/7 filer rotation.

    Args:
        cik: 機構 CIK(10 碼 zero-pad 字串,例 "0001067983")。
        quarter_end: 季底(YYYY-MM-DD);省略 = 自動取該機構最新一季。
        limit: 最多回傳幾筆(1-1000,預設 100)。**JSON 模式後端硬上界 1000**,傳更大值會被
            靜默截到 1000。

    Returns:
        list[dict],欄位同 `list_13f_holders`(filer_cik、filer_name、ticker、cusip、
        quarter_end、shares、market_value(USD)、change_in_shares、change_type)。
        ticker 可能為 null(CUSIP 對不到 watchlist 的非美股 / 衍生品)。
    """
    params: dict[str, Any] = {"cik": cik, "limit": limit}
    if quarter_end is not None:
        params["quarter_end"] = quarter_end
    return await api.get("/api/13f/portfolio", params=params)


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def list_13f_top_buyers(
    ticker: str,
    quarter_end: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """列出某股票本季「增持 / 新進最多」的機構(change_in_shares 由大到小)。

    Data cadence: quarterly holdings with 45-day SEC filing lag; refreshed on a daily 1/7 filer rotation.

    Args:
        ticker: 美股代號(自動轉大寫)。
        quarter_end: 季底(YYYY-MM-DD);省略 = 自動取該股票最新一季。
        limit: 最多回傳幾筆(1-1000,預設 50)。**JSON 模式後端硬上界 1000**,傳更大值會被
            靜默截到 1000。

    Returns:
        list[dict],欄位同 `list_13f_holders`,依 change_in_shares 遞減排序。
    """
    params: dict[str, Any] = {"ticker": ticker.upper(), "limit": limit}
    if quarter_end is not None:
        params["quarter_end"] = quarter_end
    return await api.get("/api/13f/top-buyers", params=params)


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def list_13f_top_sellers(
    ticker: str,
    quarter_end: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """列出某股票本季「減持 / 出清最多」的機構(change_in_shares 由小到大,最負在前)。

    Data cadence: quarterly holdings with 45-day SEC filing lag; refreshed on a daily 1/7 filer rotation.

    Args:
        ticker: 美股代號(自動轉大寫)。
        quarter_end: 季底(YYYY-MM-DD);省略 = 自動取該股票最新一季。
        limit: 最多回傳幾筆(1-1000,預設 50)。**JSON 模式後端硬上界 1000**,傳更大值會被
            靜默截到 1000。

    Returns:
        list[dict],欄位同 `list_13f_holders`,依 change_in_shares 遞增排序。
    """
    params: dict[str, Any] = {"ticker": ticker.upper(), "limit": limit}
    if quarter_end is not None:
        params["quarter_end"] = quarter_end
    return await api.get("/api/13f/top-sellers", params=params)


# ============================================================
# Corporate actions(現金股息 / 股票分割,Alpha Vantage)
# ============================================================


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def list_dividends(
    ticker: str,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """列出某股票的現金股息(配息)歷史,依除息日(ex_dividend_date)由新到舊。

    給除息跳空判讀,或用 amount 自算還原價(prices 無 adj_close)。

    Data cadence: refreshed weekly (Sunday).

    Args:
        ticker: 美股代號(自動轉大寫)。
        limit: 最多回傳幾筆(1-1000,預設 100)。
        offset: 跳過前 N 筆,分頁用(>=0,預設 0)。

    Returns:
        list[dict],每筆含 ticker、ex_dividend_date、declaration_date、record_date、
        payment_date(皆 YYYY-MM-DD,後三者資料源常缺 → null)、amount(每股配息金額,
        USD)。查無資料回空 list。
    """
    return await api.get(
        f"/api/dividends/{ticker.upper()}",
        params={"limit": limit, "offset": offset},
    )


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def list_splits(
    ticker: str,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """列出某股票的股票分割(split)歷史,依生效日(effective_date)由新到舊。

    給在分割點還原歷史價格序列(prices 無 adj_close)。

    Data cadence: refreshed weekly (Sunday).

    Args:
        ticker: 美股代號(自動轉大寫)。
        limit: 最多回傳幾筆(1-1000,預設 100)。
        offset: 跳過前 N 筆,分頁用(>=0,預設 0)。

    Returns:
        list[dict],每筆含 ticker、effective_date(YYYY-MM-DD)、split_factor
        (= 新股數 / 舊股數:2:1 正向分割 → 2.0,1:10 反向分割 → 0.1)。
        查無資料回空 list。
    """
    return await api.get(
        f"/api/splits/{ticker.upper()}",
        params={"limit": limit, "offset": offset},
    )


# ============================================================
# Earnings calendar(即將公布財報日,Alpha Vantage)
# ============================================================


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def list_earnings(
    ticker: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """列出某公司(即將公布)的財報日清單,依公布日(report_date)由舊到新。

    給 swing trader 避開 earnings gap、安排進出場(單 ticker 視角)。要掃整個市場
    某區間誰公布財報改用 `get_earnings_calendar`。

    Data cadence: calendar refreshed daily 03:00 UTC; report dates are vendor estimates; estimate_eps is a vendor consensus EPS (~3/4 of entries) — cite with attribution; no ratings/revenue forecasts.

    Args:
        ticker: 美股代號(自動轉大寫)。
        start: 只取此公布日(含)之後(YYYY-MM-DD);省略 = 不設下界。
        end: 只取此公布日(含)之前(YYYY-MM-DD);省略 = 不設上界。
        limit: 最多回傳幾筆(1-1000,預設 100)。

    Returns:
        list[dict],每筆含 ticker、report_date(預定公布日,YYYY-MM-DD)、
        fiscal_date_ending(對應財報期末,YYYY-MM-DD)、estimate_eps(共識每股盈餘,
        常缺 → null)、currency(ISO 4217,常缺 → null)、report_time
        ('pre-market' / 'post-market' / null)。查無資料回空 list。
    """
    params: dict[str, Any] = {"limit": limit}
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    return await api.get(f"/api/earnings/{ticker.upper()}", params=params)


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def get_earnings_calendar(
    start: str | None = None,
    end: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """掃整個市場某日期區間內即將公布財報的公司(跨 ticker),依 (report_date, ticker) 由舊到新。

    回答「下週 / 某區間有哪些公司公布財報」。要單一公司的財報日改用 `list_earnings`。

    Data cadence: calendar refreshed daily 03:00 UTC; report dates are vendor estimates; estimate_eps is a vendor consensus EPS (~3/4 of entries) — cite with attribution; no ratings/revenue forecasts.

    Args:
        start: 區間起始公布日(含,YYYY-MM-DD);省略 = 不設下界。
        end: 區間結束公布日(含,YYYY-MM-DD);省略 = 不設上界。
        limit: 最多回傳幾筆(1-2000,預設 500)。

    Returns:
        list[dict],欄位同 `list_earnings`(ticker、report_date、fiscal_date_ending、
        estimate_eps、currency、report_time)。查無資料回空 list。
    """
    params: dict[str, Any] = {"limit": limit}
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    return await api.get("/api/earnings/calendar", params=params)


# ============================================================
# Earnings call transcripts(法說會逐字稿,Alpha Vantage)
# ============================================================


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def get_earnings_transcript(
    ticker: str,
    quarter: str | None = None,
    offset: int = 0,
    limit: int = 40,
    speaker: str | None = None,
) -> dict[str, Any]:
    """取得某公司某季法說會(earnings call)逐字稿的分頁段落 + 該場 meta。

    法說會逐字稿是「管理層語氣 / 經營展望」的第一手長文本(發言者 + 職稱 + 每段內文 +
    每段情緒分)。一場法說會通常 ~60-80 段、全文約 4-5 萬字元 —— **本工具預設只回 40 段
    (limit=40)以保護你的 context**;要看全文請翻頁:offset=40 取下一頁,以此類推
    (回傳的 `segments_total` 是整場段數,據此判斷還有幾頁)。只關心某發言者(如只看 CEO)
    可帶 `speaker` 做部分比對過濾。

    `quarter` 省略時自動取該公司**最新一季**(先打 list 拿最新季,再取那季逐字稿);
    若該公司完全沒有逐字稿則誠實報錯(來源涵蓋以中大型股為主,小型股常無)。

    `sentiment`(每段情緒分)是 **vendor(Alpha Vantage)模型**對該段算的分數,**非本平台
    計算**;當訊號參考即可,別當精確值。

    Data cadence: available ~T+1 after the call, quarterly.

    Args:
        ticker: 美股代號(自動轉大寫)。
        quarter: AV calendar quarter,如 "2025Q4"(大小寫不拘,前後空白會自動 strip);
            **省略 / 空字串 / 純空白 = 自動取最新一季**。
        offset: 跳過前 N 段(分頁用,>=0,預設 0)。
        limit: 本頁段落數上限(1-500,預設 40 以保護 context;要全文請翻頁)。
        speaker: 只看某發言者,對 speaker 做部分比對(case-insensitive,如 "cook");省略 = 全部。

    Returns:
        dict,含 ticker、quarter、segments_total(整場段數,不受分頁/過濾影響)、fetched_at、
        segments(本頁段落 list,每段含 seq、speaker、speaker_title、content、sentiment)、
        available_quarters(該公司可用季別,最多前 8 筆,新到舊)。該公司無任何逐字稿 →
        tool error(誠實說無資料)。
    """
    upper = ticker.upper()

    # 先取可用季別清單:用來(1)quarter 省略時挑最新季、(2)無資料時誠實報錯、
    # (3)塞進回傳的 available_quarters 給 agent 知道還有哪幾季。
    summaries = await api.get(f"/api/companies/{upper}/transcripts")
    if not summaries:
        raise ToolError(
            f"No earnings call transcripts on file for {upper}. "
            "Transcript coverage skews to mid/large-cap names; smaller companies often "
            "have none. Verify the ticker with search_companies if unsure."
        )

    available_quarters = [s["quarter"] for s in summaries[:8]]
    # quarter 有給且不是純空白才用它(先 strip 去掉前後空白再 upper,避免 " 2025q4 "
    # 帶空白打 detail 打不到);空字串 / 純空白視同省略 → 取最新一季。
    target_quarter = (
        quarter.strip().upper() if quarter and quarter.strip() else summaries[0]["quarter"]
    )

    params: dict[str, Any] = {"offset": offset, "limit": limit}
    if speaker is not None:
        params["speaker"] = speaker
    detail = await api.get(
        f"/api/companies/{upper}/transcripts/{target_quarter}", params=params
    )
    detail["available_quarters"] = available_quarters
    return detail


# ============================================================
# News + sentiment(新聞情緒,Alpha Vantage —— vendor 聚合源)
# ============================================================


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def get_company_news(
    ticker: str,
    days: int = 7,
    min_relevance: float = 0.5,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """取得某公司近 N 天的新聞 + 情緒,published_at 由新到舊。

    **這是 vendor 聚合源(Alpha Vantage NEWS_SENTIMENT),非第一手 SEC 資料** —— 來源品質
    參差(聚合器混入低品質源),所以用 `relevance`(該文對此 ticker 的相關度,0-1)過濾:
    **min_relevance >= 0.5 才算有訊號**,再低多半是蹭關鍵字的雜訊。`sentiment` 欄位是 AV
    模型分(vendor metric,非本平台計算)。

    Data cadence: 每 4 小時更新一次,標注為非即時(本平台仍是 EOD 定位,新聞區塊以抓取時間
    為準,不是即時 feed)。

    Args:
        ticker: 美股代號(自動轉大寫)。
        days: 只取近 N 天的新聞(1-90,預設 7)。
        min_relevance: 相關度下限(0-1,預設 0.5);低於此值不回。
        limit: 最多回傳幾筆(1-200,預設 20)。

    Returns:
        list[dict],每筆含 title、url(原文連結)、source、source_domain、published_at、
        summary、overall_sentiment / overall_label(該文整體情緒)、relevance(對此 ticker
        相關度)、ticker_sentiment / ticker_label(對此 ticker 的情緒)。查無資料回空 list。
    """
    return await api.get(
        f"/api/companies/{ticker.upper()}/news",
        params={"days": days, "min_relevance": min_relevance, "limit": limit},
    )


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def get_market_news(
    topic: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """取得全市場最新新聞 + 情緒(不掛單一 ticker),published_at 由新到舊。

    **vendor 聚合源(Alpha Vantage),非第一手資料**;情緒分為 AV 模型分。給「市場現在在
    談什麼」的總覽;要單一公司的新聞用 `get_company_news`。

    Data cadence: 每 4 小時更新一次,標注為非即時。

    Args:
        topic: 按 AV 主題過濾;省略 = 全市場。**大小寫敏感的精確比對** —— 後端拿你傳的字串
            去跟文章 topics[].topic 逐字比對,對不上(含大小寫 / 拼字不同)就靜默回空 list,
            **不是報錯**。存的是 Alpha Vantage 的顯示標籤(Title Case,非小寫代碼),合法值:
            "Blockchain", "Earnings", "IPO", "Mergers & Acquisitions", "Financial Markets",
            "Economy - Fiscal Policy", "Economy - Monetary Policy", "Economy - Macro/Overall",
            "Energy & Transportation", "Finance", "Life Sciences", "Manufacturing",
            "Real Estate & Construction", "Retail & Wholesale", "Technology"。不確定當前實際
            有哪些值,先不帶 topic 呼叫一次、看回傳每篇的 topics[].topic 再原樣帶回來。
        limit: 最多回傳幾筆(1-200,預設 20)。

    Returns:
        list[dict],每筆含 title、url、source、source_domain、published_at、summary、
        overall_sentiment / overall_label、topics(AV 主題標註 [{topic, relevance}])。
        查無資料回空 list。
    """
    params: dict[str, Any] = {"limit": limit}
    if topic is not None:
        params["topic"] = topic
    return await api.get("/api/market/news", params=params)


# ============================================================
# ETF(概況 / 成分股 / 反查持有者,Alpha Vantage)
# ============================================================


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def get_etf_profile(ticker: str) -> dict[str, Any]:
    """取得單一 ETF 的層級 metadata(淨資產 / 費用率 / 配息率 / 是否槓桿等)。

    Data cadence: monthly refresh; fixed universe of 25 large ETFs.

    Args:
        ticker: ETF 代號(自動轉大寫,例 SPY、QQQ)。

    Returns:
        dict,欄位含 net_assets(淨資產,USD)、net_expense_ratio、portfolio_turnover、
        dividend_yield(此三者為 0-1 小數,乘 100 才是百分比)、inception_date
        (YYYY-MM-DD)、leveraged(bool,是否槓桿型)、updated_at(ISO datetime,本表
        最近刷新時間)。多數欄位 nullable。查無此 ETF → tool error(後端 404)。
    """
    return await api.get(f"/api/etf/{ticker.upper()}/profile")


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def list_etf_holdings(
    ticker: str,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """列出某 ETF 的成分股 + 權重,依權重(weight)由大到小(top holdings 先),分頁。

    Data cadence: monthly refresh; fixed universe of 25 large ETFs.

    Args:
        ticker: ETF 代號(自動轉大寫,例 SPY)。
        limit: 最多回傳幾筆(1-1000,預設 100)。
        offset: 跳過前 N 筆,分頁用(>=0,預設 0)。

    Returns:
        list[dict],每筆含 etf_ticker、holding_symbol(成分標的代號,可能是現金 / 海外 /
        未收錄標的)、description(成分名稱,常缺 → null)、weight(占該 ETF 淨值比例,
        0-1 小數而非百分比;null 排最後)。查無資料回空 list。
    """
    return await api.get(
        f"/api/etf/{ticker.upper()}/holdings",
        params={"limit": limit, "offset": offset},
    )


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def list_etfs_holding_ticker(
    ticker: str,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """反查:有哪些 ETF 持有某個 symbol,依該 symbol 在各 ETF 的權重由大到小,分頁。

    跟 `list_etf_holdings`(由 ETF 看成分)方向相反:這裡由個股 / 標的反查持有它的 ETF,
    給被動資金流判讀(哪些 ETF 重壓這檔)。

    Data cadence: monthly refresh; fixed universe of 25 large ETFs.

    Args:
        ticker: 被持有的 symbol(個股或其他標的,自動轉大寫,例 AAPL)。
        limit: 最多回傳幾筆(1-1000,預設 100)。
        offset: 跳過前 N 筆,分頁用(>=0,預設 0)。

    Returns:
        list[dict],欄位同 `list_etf_holdings`(etf_ticker、holding_symbol、description、
        weight 為 0-1 小數)。查無資料回空 list。
    """
    return await api.get(
        f"/api/etf/holders/{ticker.upper()}",
        params={"limit": limit, "offset": offset},
    )


# ============================================================
# Macro(總經 / 商品 / 指數 時間序列,單位看 catalog)
# ============================================================


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def list_macro_series(category: str | None = None) -> list[dict[str, Any]]:
    """列出可查的總經 / 商品 / 指數 series 目錄(自我描述字典),依 (category, series_id) 排序。

    這是查觀測值前的「目錄」步驟:先在這裡挑出 series_id 與看它的 `unit`,再帶 series_id
    去 `get_macro_series` 取時間序列(觀測值本身不帶單位)。

    Data cadence: economic/commodity series monthly; index series (SPX/NDX/VIX) daily (rollout in progress).

    Args:
        category: 按類別過濾,'macro' / 'commodity' / 'index';省略 = 全部。

    Returns:
        list[dict],每筆含 series_id(查觀測值用的代碼,區分大小寫)、name(人類可讀)、
        unit(觀測值單位,如 'percent' / 'USD' / 'index',常缺 → null)、frequency
        ('monthly' / 'quarterly' / 'daily' / 'annual' 等)、category、updated_at
        (ISO datetime)。
    """
    params: dict[str, Any] = {}
    if category is not None:
        params["category"] = category
    return await api.get("/api/macro/series", params=params)


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def get_macro_series(
    series_id: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = 2000,
    order: str = "desc",
) -> list[dict[str, Any]]:
    """Returns newest-first by default; pass order='asc' for chart-style series.

    取得某 series 的觀測時間序列,邊界 inclusive。**MCP 端預設 order='desc'(最新在前)** ——
    問「最新 VIX / 最近一筆 CPI」時 limit=1 即拿到最新值;要畫時間序列圖 / 算移動平均請改
    order='asc'(最舊在前)。

    series_id 區分大小寫,請用 `list_macro_series` 目錄拿到的原值(例 CPI、
    TREASURY_YIELD_10YEAR、WTI、INDEX_VIX)。注意:**value 的單位不在本回應裡** ——
    要去 `list_macro_series` 查該 series 的 `unit` 欄位。

    Data cadence: economic/commodity series monthly; index series (SPX/NDX/VIX) daily (rollout in progress).

    Args:
        series_id: series 代碼(區分大小寫,來自 `list_macro_series`)。
        start: 起始日(含,YYYY-MM-DD);省略 = 不設下界。
        end: 結束日(含,YYYY-MM-DD);省略 = 不設上界。
        limit: 最多回傳幾筆(1-10000,預設 2000)。
        order: 'desc'(最新在前,預設)或 'asc'(最舊在前,畫圖 / 算 MA 用)。

    Returns:
        list[dict],每筆含 series_id、date(YYYY-MM-DD)、value(數值,單位見目錄的
        `unit`)。series_id 不存在或區間無資料回空 list。
    """
    params: dict[str, Any] = {"limit": limit, "order": order}
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    return await api.get(f"/api/macro/series/{series_id}", params=params)


# ============================================================
# Market snapshot(全市場漲跌榜 + IPO 行事曆,Alpha Vantage)
# ============================================================


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def get_market_movers(date: str | None = None) -> dict[str, Any]:
    """取得某交易日的全市場漲幅榜 / 跌幅榜 / 成交最熱榜(各 top 20)。

    全市場「當天誰在動」的快照,不掛單一 ticker。三個 category 的含義:
      - gainers(漲幅榜):當日 change_pct 最高的 20 檔(漲最多)。
      - losers(跌幅榜):當日 change_pct 最低的 20 檔(跌最多)。
      - most_active(成交最熱):當日成交量最大的 20 檔(不論漲跌)。
    要單一公司的價格用 get_latest_price / list_daily_prices。資料來源 Alpha Vantage
    TOP_GAINERS_LOSERS。

    Data cadence: 每交易日收盤後更新;非即時(EOD)。

    Args:
        date: 交易日(YYYY-MM-DD);**省略 = 自動取最新一天(建議不帶,直接拿最新)**。
            帶了某日但該日無資料 → tool error(後端 404,改成不帶 date 取最新)。

    Returns:
        dict,含 date(該快照交易日 YYYY-MM-DD)、last_updated(該日刷新時間 ISO,可能
        null)、gainers / losers / most_active 三個 list。每筆含 rank(1-20)、ticker、
        price(USD)、change_amount(相對前一交易日的價格變動,USD)、change_pct(變動
        百分比數值,如 5.23 = +5.23%,**非 0-1 小數**)、volume(當日成交量)。
    """
    params: dict[str, Any] = {}
    if date is not None:
        params["date"] = date
    return await api.get("/api/market/movers", params=params)


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def get_ipo_calendar(
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict[str, Any]]:
    """取得 IPO 行事曆(近期 / 即將上市新股),依掛牌日(ipo_date)由舊到新。

    一列一檔即將 / 近期 IPO。資料來源 Alpha Vantage IPO_CALENDAR。

    Data cadence: 每日更新;IPO 日期與價格區間可能變動,0/null = 未定價。

    Args:
        from_date: 起始掛牌日(含,YYYY-MM-DD);**省略 = 今天**。
        to_date: 結束掛牌日(含,YYYY-MM-DD);**省略 = 今天起 90 天**。
            (兩個都省略才套「今天 ~ +90 天」預設視窗;只帶一邊就只約束那一邊。)

    Returns:
        list[dict],每筆含 symbol、ipo_date(預定掛牌日 YYYY-MM-DD)、name、
        price_range_low / price_range_high(定價區間,USD,**null = 尚未定價**)、
        currency(ISO 4217)、exchange(掛牌交易所)。查無資料回空 list。
    """
    params: dict[str, Any] = {}
    if from_date is not None:
        params["from_date"] = from_date
    if to_date is not None:
        params["to_date"] = to_date
    return await api.get("/api/market/ipo-calendar", params=params)


# ============================================================
# Screener(跨 ticker 篩選)
# ============================================================


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def screen_insider_buys(
    transaction_code: str = "P",
    since_days: int = 90,
    ticker_contains: str | None = None,
    insider_title_contains: str | None = None,
    market_cap_min: float | None = None,
    market_cap_max: float | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """跨整個 universe 篩選內部人 open-market 交易(預設買進),回答「最近誰在買什麼」。

    跟 `list_insider_trades`(單 ticker)互補:這裡掃全市場過濾出符合條件的列,依
    transaction_date DESC, usd_value DESC 排序。注意 market_cap 由最新股價估算,PROD 上
    prices 資料未齊時可能多為 null。

    Data cadence: market_cap covers ~7k of ~20k tickers — missing means not-covered, not zero; cross-market screen over EOD data.

    Args:
        transaction_code: SEC Form 4 transaction code,預設 "P"(open market 買進);"S" = 賣出。
        since_days: 只看過去這幾天內的交易(0-3650,預設 90)。
        ticker_contains: 模糊比對 ticker(case-insensitive);省略 = 不過濾。
        insider_title_contains: 模糊比對 insider 職稱(例 "CEO"、"Director");省略 = 不過濾。
        market_cap_min: USD 市值下限;省略 = 不過濾。
        market_cap_max: USD 市值上限;省略 = 不過濾。
        limit: 最多回傳幾筆(1-1000,預設 100)。

    Returns:
        dict,shape = {"items": [...], "count": N}。每筆 item 含 transaction_date、
        ticker、company_name、market_cap(USD,可能 null)、insider_name、insider_title、
        shares、price_per_share(USD)、usd_value(USD,可能 null)。
    """
    params: dict[str, Any] = {
        "transaction_code": transaction_code,
        "since_days": since_days,
        "limit": limit,
    }
    if ticker_contains is not None:
        params["ticker_contains"] = ticker_contains
    if insider_title_contains is not None:
        params["insider_title_contains"] = insider_title_contains
    if market_cap_min is not None:
        params["market_cap_min"] = market_cap_min
    if market_cap_max is not None:
        params["market_cap_max"] = market_cap_max
    return await api.get("/api/screener/insider-buys", params=params)


# ============================================================
# Analysis(四面向紅綠燈彙整 —— 帶解讀,給 agent 先看結論)
# ============================================================


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def get_analysis(ticker: str) -> dict[str, Any]:
    """取得四面向紅綠燈分析(基本面/籌碼面/技術面/期權面結論 + 綜合總評)。

    「先看結論」入口:後端把原始數據整合成每面向一句中文結論 + verdict + 綜合總評。
    跟 `get_objective_report`(純數據無解讀)互補:要結論用本工具,要原料用 report。

    **注意 overall_summary 只列 bullish 與 bearish 面向,verdict="neutral" 的面向不會
    出現在總評**;某些底層偏空訊號(例如內部人大量賣出)會被規則降級為 neutral。要對
    使用者誠實呈現風險,不應只讀 overall_verdict/overall_summary,務必逐一檢視每個 lens
    的 signals(尤其 neutral 面向),必要時用 list_insider_trades / list_13f_holders 取
    原始數字自行覆寫。各面向 as_of 可能落差數週至一季(技術面最新、基本面上一季、13F 約
    90 天延遲),overall 是跨時間軸結論的平均。signals[].value 是含措辭的 display string
    (如 "+17%"),非機器可讀數值 —— 要門檻判斷 / 跨標的比較請改用對應細項 tool。

    永遠回 200(資料缺的面向 verdict="na" 並從綜合分母剔除,不像 get_company 回 404)。
    **overall_verdict 永不 "na"**(全缺退為 "neutral",lenses_scored=0)。

    Data cadence: valuation snapshot self-computed daily 16:00 UTC from latest close x shares; market_cap covers ~7k of ~20k tickers — missing means not-covered, not zero.

    Args:
        ticker: 美股代號(大小寫不拘,自動轉大寫)。

    Returns:
        dict:ticker、overall_verdict("bullish"|"neutral"|"bearish")、overall_summary、
        lenses_scored(0-4,na 不計)、lenses[](固定 4 個,序 fundamental→chips→technical
        →options)。每 lens 含 key、name、verdict、summary、as_of(ISO 或 null)、
        signals[]{label, value(display string,無資料 "—"), verdict}。
    """
    return await api.get(f"/api/analysis/{ticker.upper()}")


# ============================================================
# Objective report(客觀數據包 —— 純數據,給 agent 自行解讀)
# ============================================================


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def get_objective_report(
    ticker: str,
    sections: str | None = None,
    statements_limit: int = 8,
    insider_limit: int = 20,
    holders_limit: int = 10,
    filings_limit: int = 5,
    recent_price_bars: int = 30,
) -> dict[str, Any]:
    """一次取得單一公司「客觀數據包」:估值 + 近 N 期三表 + 內部人 + 13F + 價格摘要 +
    期權摘要 + 近期 filing 章節標題清單。**純數據,無任何解讀 / 評分**,給你(agent)自行分析。

    跟 `get_analysis`(四面向紅綠燈 + 中文結論,**有解讀**)互補:要結論用 get_analysis;
    要「給我原料我自己判斷」用本工具,免逐一打 8 個 endpoint。

    payload 已為 LLM context 控制:不含 filing 內文(只給章節標題 + section_count,要內文
    再用 `get_filing_section`);13F / 期權只給彙總 + top-N;價格只給摘要 + 最近數十根日 K。
    對 token 敏感時建議用 sections= 只挑需要的塊,別無腦全取。

    Data cadence: valuation snapshot self-computed daily 16:00 UTC from latest close x shares; market_cap covers ~7k of ~20k tickers — missing means not-covered, not zero.

    Args:
        ticker: 美股代號(自動轉大寫)。
        sections: 逗號分隔只取部分塊以省 token,可選:company, overview, financials,
            insider, institutional_13f, price_summary, options_summary, filings;省略 = 全取。
        statements_limit: 三表各取近 N 期(1-20,預設 8)。
        insider_limit: 內部人交易筆數(1-100,預設 20)。
        holders_limit: 13F top holders 筆數(1-50,預設 10)。
        filings_limit: 近 N 份 filing 列章節目錄(1-20,預設 5)。
        recent_price_bars: 回最近 N 根日 K(1-120,預設 30)。

    Returns:
        dict,頂層含 ticker、generated_at、company,與八個 {source, as_of, ...} 信封塊:
        source 標第一手來源(中性表名)、as_of 標資料最新日期(無資料 → as_of=null、空 list)。
        price_summary / options_summary 的彙總值都附算式輸入(latest_close、week_52_high/low、
        put_volume/call_volume)供你自行驗算;options contract_count 等於各 expiration 之和。
        無此 ticker 不報錯,而是各塊空(company=null)。
    """
    params: dict[str, Any] = {
        "statements_limit": statements_limit,
        "insider_limit": insider_limit,
        "holders_limit": holders_limit,
        "filings_limit": filings_limit,
        "recent_price_bars": recent_price_bars,
    }
    if sections is not None:
        params["sections"] = sections
    return await api.get(f"/api/report/{ticker.upper()}", params=params)


# ============================================================
# Overview(估值快照 —— company_overview)
# ============================================================


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def get_overview(ticker: str) -> dict[str, Any]:
    """取得公司估值快照的原始數據:市值 / 本益比家族 / Beta / 52 週高低 / 均線 / 分析師目標價。

    純數據快照(貴賤、技術強弱由你判讀)。資料源 Alpha Vantage OVERVIEW(每日刷新)。要逐期
    財報數字用 get_income_statements 等;要四面向結論用 get_analysis。

    Data cadence: valuation snapshot self-computed daily 16:00 UTC from latest close x shares; market_cap covers ~7k of ~20k tickers — missing means not-covered, not zero.

    Args:
        ticker: 美股代號(自動轉大寫)。不知道精確 ticker 先用 search_companies。

    Returns:
        dict。金額(USD 整數,nullable):market_cap, shares_outstanding, float_shares,
        free_float_market_cap, ebitda, revenue_ttm, gross_profit_ttm。估值比率(float):
        float_pct(自由流通占比 0-1), pe_ratio, forward_pe, peg_ratio, price_to_book,
        price_to_sales_ttm, ev_to_ebitda, ev_to_revenue。每股:eps, diluted_eps_ttm, book_value。
        配息:dividend_per_share, dividend_yield(**0-1 小數**,非百分比)。獲利能力(皆 **0-1
        小數**):profit_margin, operating_margin_ttm, return_on_assets_ttm, return_on_equity_ttm。
        風險/技術:beta, week_52_high, week_52_low, ma_50, ma_200。analyst_target_price。
        latest_quarter(財報期末 YYYY-MM-DD)、updated_at(本表刷新時間 ISO)。多數 nullable。
        尚未被 overview ETL 覆蓋 → tool error(後端 404)。
    """
    return await api.get(f"/api/overview/{ticker.upper()}")


# ============================================================
# FINRA short market data
# ============================================================


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def get_short_interest(
    ticker: str,
    limit: int = 24,
) -> list[dict[str, Any]]:
    """取得 FINRA 未平倉空單歷史(每月兩次),最新 settlement date 在前。

    這是 open short position,不是每日 short-sale volume。short_percent_float 是
    0-100 百分比數值(12.3 = 12.3%),只有資料庫有可信 float_shares 時才回值;
    缺分母回 null,不拿 shares outstanding 冒充。limit 預設 24 = 約一年。
    """
    return await api.get(
        f"/api/shorts/interest/{ticker.upper()}", params={"limit": limit}
    )


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def get_short_volume(
    ticker: str,
    days: int = 30,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """取得 FINRA 場外每日 short-sale transaction volume。

    short_volume_percent 是 FINRA-reported off-exchange short volume / total volume
    的 0-100 百分比。它是 flow metric,不是未平倉空單,也不含交易所成交量。
    """
    return await api.get(
        f"/api/shorts/volume/{ticker.upper()}",
        params={"days": days, "limit": limit},
    )


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def screen_high_short_interest(
    min_short_percent_float: float = 10.0,
    min_days_to_cover: float = 0.0,
    limit: int = 50,
) -> dict[str, Any]:
    """掃描最新 FINRA settlement universe,依 short % float 由高到低排序。

    適合找潛在 squeeze / crowded-short 名單。結果只包含有可信 float_shares
    分母的標的;short % float 高不等於一定會軋空,應再配合 days_to_cover、流動性、
    價格與催化劑判讀。
    """
    return await api.get(
        "/api/shorts/screener",
        params={
            "min_short_percent_float": min_short_percent_float,
            "min_days_to_cover": min_days_to_cover,
            "limit": limit,
        },
    )


# ============================================================
# Options(期權 EOD —— options_eod)
# ============================================================


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def get_options_chain(
    ticker: str,
    as_of: str | None = None,
    expiration: str | None = None,
    option_type: Literal["call", "put"] | None = None,
    limit: int = 250,
) -> list[dict[str, Any]]:
    """取得某標的某交易日的期權鏈(EOD 報價 + IV + greeks),依 (到期日, 履約價, call/put) 排序。

    一列 = 一個 OCC 合約在該交易日的 EOD snapshot。**全鏈可達上千合約,預設 limit=250 防爆
    context**;建議先用 get_option_expirations 拿到期日,再帶 expiration 過濾。ticker 用 AV
    symbol,不保證對得上 companies.ticker(指數選擇權、BRK.B/BRK-B 命名差異)。查無 →
    回空 list;若是符號寫法問題,試 dot/dash 兩種(BRK.B vs BRK-B)或先 search_companies。

    Data cadence: EOD chains for the prior trading day, fetched Mon-Fri 04:00 UTC.

    Args:
        ticker: 標的代號(AV symbol,自動轉大寫)。
        as_of: EOD 交易日(YYYY-MM-DD);省略 = 該標的最新交易日。
        expiration: 只取此到期日(YYYY-MM-DD);省略 = 全到期。
        option_type: "call" 或 "put";省略 = 兩者皆回。
        limit: 合約數上限(1-5000,預設 250)。

    Returns:
        list[dict],每筆含 contract_id(OCC)、date、underlying(資料欄位,即標的代號)、
        expiration、strike、option_type、last、mark、bid、bid_size、ask、ask_size、volume、
        open_interest、implied_volatility、delta、gamma、theta、vega、rho。**數值欄(strike/
        報價/IV/各 greek)以 JSON 字串回傳(如 "200.0000"、"-0.019830"),做數學前先轉 float;
        greeks 可為負**;bid_size/ask_size/volume/open_interest 為整數。null = 報價/greek 缺失,
        勿當 0 納入計算。無資料回空 list。
    """
    # API 端 query 參數名仍是 underlying(沒改 API);這裡只把工具參數統一成 ticker。
    params: dict[str, Any] = {"underlying": ticker.upper(), "limit": limit}
    if as_of is not None:
        params["as_of"] = as_of
    if expiration is not None:
        params["expiration"] = expiration
    if option_type is not None:
        params["option_type"] = option_type
    return await api.get("/api/options/chain", params=params)


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def get_option_expirations(
    ticker: str, as_of: str | None = None
) -> list[dict[str, Any]]:
    """列出某標的某交易日可選的到期日 + 各到期合約數,ascending expiration。

    給挑 expiration 用 —— 先拿到期日,再帶去 get_options_chain 過濾,避免一次拉整鏈。

    Data cadence: EOD chains for the prior trading day, fetched Mon-Fri 04:00 UTC.

    Args:
        ticker: 標的代號(AV symbol,自動轉大寫)。
        as_of: EOD 交易日(YYYY-MM-DD);省略 = 最新交易日。

    Returns:
        list[dict],每筆含 expiration(YYYY-MM-DD)、contract_count。無資料回空 list。
    """
    # API 端 query 參數名仍是 underlying(沒改 API);這裡只把工具參數統一成 ticker。
    params: dict[str, Any] = {"underlying": ticker.upper()}
    if as_of is not None:
        params["as_of"] = as_of
    return await api.get("/api/options/expirations", params=params)


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def get_option_contract_history(
    contract_id: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    """取得單一 OCC 合約的逐日 EOD 時間序列(報價 / IV / greeks 隨時間),ascending date。

    Data cadence: EOD chains for the prior trading day, fetched Mon-Fri 04:00 UTC.

    Args:
        contract_id: OCC 合約代號,區分大小寫原樣(例 "AAPL260605C00200000")。可從
            get_options_chain 取得。
        start: 起始日(YYYY-MM-DD,inclusive);省略 = 不設下界。
        end: 結束日(YYYY-MM-DD,inclusive);省略 = 不設上界。
        limit: 天數上限(1-10000,預設 2000)。

    Returns:
        list[dict],欄位同 get_options_chain(數值欄為 JSON 字串)。無資料回空 list。
    """
    params: dict[str, Any] = {"limit": limit}
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    return await api.get(f"/api/options/contract/{contract_id}", params=params)


# ============================================================
# 13F filer lookup(機構名 → CIK)
# ============================================================


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def search_institutions(q: str, limit: int = 20) -> list[dict[str, Any]]:
    """以機構名稱或 CIK 關鍵字模糊搜尋 13F filer(typeahead),適合「只知道機構名字」時。

    拿到 cik 後帶去 list_13f_portfolio 看該機構整個持倉。

    Data cadence: quarterly holdings with 45-day SEC filing lag; refreshed on a daily 1/7 filer rotation.

    Args:
        q: 機構名稱或 CIK 關鍵字(至少 1 字元,例 "berkshire"、"1067983")。
        limit: 最多回傳幾筆(1-50,預設 20)。

    Returns:
        list[dict],每筆含 cik、name、first_seen(YYYY-MM-DD 或 null)、latest_quarter
        (該 filer 最新持倉季 YYYY-MM-DD 或 null)。無命中回空 list。
    """
    return await api.get("/api/13f/institutions/search", params={"q": q, "limit": limit})


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def get_institution(cik: str) -> dict[str, Any]:
    """以精確 CIK 取得單一 13F filer 基本資料(名稱 / first_seen / 最新持倉季)。

    Data cadence: quarterly holdings with 45-day SEC filing lag; refreshed on a daily 1/7 filer rotation.

    Args:
        cik: 機構 CIK(10 碼 zero-pad 字串,例 "0001067983")。

    Returns:
        dict,含 cik、name、first_seen、latest_quarter。查無 → tool error(後端 404)。
    """
    return await api.get(f"/api/13f/institutions/{cik}")


# ============================================================
# ETF sector weights
# ============================================================


@mcp.tool(annotations=_READONLY_ANNOTATIONS)
async def list_etf_sectors(ticker: str) -> list[dict[str, Any]]:
    """取得某 ETF 的 GICS sector 權重,依權重由大到小。給判讀 ETF 的類股配置。

    Data cadence: monthly refresh; fixed universe of 25 large ETFs.

    Args:
        ticker: ETF 代號(自動轉大寫,例 SPY)。

    Returns:
        list[dict],每筆含 etf_ticker、sector(GICS 類股名)、weight(**0-1 小數,以 JSON
        字串回傳**,如 "0.37600000",占 ETF 淨值比例)。sector 約 11 個,不分頁。查無回空 list。
    """
    return await api.get(f"/api/etf/{ticker.upper()}/sectors")


# ============================================================
# Free-form readonly SQL
# ============================================================


def _json_default(value: Any) -> Any:
    """JSON serializer fallback —— date / datetime → ISO,Decimal → str。

    asyncpg Record 轉 dict 後欄位可能是 datetime / Decimal,標準 json 不會 serialize。
    """
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _record_to_dict(record: Any) -> dict[str, Any]:
    """asyncpg Record → dict,重複欄名加後綴保留全部欄位。

    直接 `dict(record)` 會讓 JOIN 出的同名欄(例 `SELECT a.id, b.id FROM a JOIN b`)後者
    蓋前者、靜默掉一欄。這裡逐欄位處理,第二個以後的同名欄依序加 `_2` / `_3` … 後綴
    (第一個維持原名),讓每一欄的值都留在結果裡。
    """
    out: dict[str, Any] = {}
    seen: dict[str, int] = {}
    for key, value in record.items():
        if key in seen:
            seen[key] += 1
            out[f"{key}_{seen[key]}"] = value
        else:
            seen[key] = 1
            out[key] = value
    return out


# Tier gating:execute_readonly_sql 是重量級 tool(自由 SQL),只開給 pro tier。
# require_scopes("tier:pro") 由 FastMCP 在元件層 enforce —— free tier(scopes 只有
# tier:free)在 list_tools 看不到此 tool,直接呼叫也會被擋(get_tool 回 None)。
# 其餘 structured tool 不加 auth,free / pro 都能用。
# 注意:auth 關閉(無 verifier)或 stdio 本機開發時框架會 skip_auth,此 gating 不生效。
@mcp.tool(auth=require_scopes("tier:pro"), annotations=_READONLY_ANNOTATIONS)
async def execute_readonly_sql(query: str) -> str:
    """跑一段 readonly SELECT,回 JSON 字串(rows + meta)。給需要彈性查詢的 agent / 分析用。

    需 pro tier(權限不足者看不到此 tool)。下 SQL 前不確定欄位?先用 `describe_table`
    自省(不帶參數 = 列出所有 table;帶 table 名 = 列出該表欄位 + 型別)。

    可查的 table:companies, institutions, institution_filings, filings, filing_sections,
    income_statements, balance_sheets, cash_flow_statements, insider_trades,
    institutional_holdings, prices_daily, prices_hourly, company_overview, options_eod,
    financials_quarantine, ingest_runs, alembic_version, dividends, splits,
    earnings_calendar, etf_profile, etf_holdings, macro_series, macro_series_meta,
    market_movers, ipo_calendar, earnings_call_transcripts, earnings_call_segments,
    news_articles, news_ticker_sentiment。實際清單以 describe_table()(不帶參數)為準。

    安全保證(三層):
      1. SQL parsing:只允許單一 SELECT / WITH ... SELECT,拒絕 INSERT/UPDATE/DELETE/DROP 等
         與 pg_sleep / copy / lo_import 等敏感函式。
      2. DB role:連線使用 `investor_db_readonly` role(僅 SELECT 權限)。
      3. Statement timeout:每段查詢 5 秒上限,複雜 query 自動 abort。

    LIMIT 自動處理(把你的查詢包成 `SELECT * FROM (<你的 SQL>) _ LIMIT n` 加硬性外層上界):
      - 沒寫頂層 LIMIT → 外層補 LIMIT 1000。
      - 頂層 LIMIT N → 外層用 min(N, 10000)。
      - 子查詢內寫 LIMIT 也無法繞過(外層上界一定生效)。
      - **只認 `LIMIT <整數>` 形式**當頂層明確上界;`LIMIT ALL`、`LIMIT $1`、非整數
        (`LIMIT 2.5` / `5e3`)、以及 ANSI `FETCH FIRST n ROWS ONLY` 都**不**被視為明確上界
        → 一律套外層預設 1000。所以要一次拿超過 1000 列,請用 `LIMIT <整數>`(別用 FETCH FIRST)。

    Output 超過 100KB 會截斷並附註記(改窄 WHERE / 縮小 LIMIT 再查)。

    Args:
        query: 要執行的 SELECT(單一 statement,不要加多個分號)。
               範例:`SELECT ticker, name FROM companies WHERE sector = 'Technology' LIMIT 50`

    Returns:
        JSON 字串,shape = {"row_count": N, "executed_query": "...", "rows": [...]}。
        **JOIN 出的同名欄**(例 `SELECT a.id, b.id ...`)不會互蓋 —— 第二個以後的同名欄會
        加 `_2` / `_3` … 後綴(`id`, `id_2`),要避免就自己下 alias(`b.id AS b_id`)。
        驗證失敗 / DB 錯誤 / 未設定 DSN → {"error": "..."}。
    """
    try:
        validate_select_only(query)
    except SQLValidationError as exc:
        return json.dumps({"error": str(exc)})

    safe_query = clamp_limit(query)

    try:
        pool = await get_pool()
    except ReadonlyDBNotConfigured as exc:
        return json.dumps({"error": str(exc)})

    try:
        async with pool.acquire() as conn:
            records = await conn.fetch(safe_query)
    except Exception as exc:  # asyncpg.PostgresError 等 —— 直接 surface 給 LLM 看。
        return json.dumps({"error": f"Query failed: {exc}"})

    rows = [_record_to_dict(r) for r in records]
    payload = json.dumps(
        {"row_count": len(rows), "executed_query": safe_query, "rows": rows},
        default=_json_default,
    )
    return truncate_payload(payload)


# information_schema 查詢用 asyncpg bind parameter($1),table 名永遠不拼進 SQL 字串,
# 所以不需要過 validate_select_only(這層的注入風險已由 parametrize 消除)。
# 只查 public schema,跟 readonly role 可見範圍一致。
_LIST_TABLES_SQL = """
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
    ORDER BY table_name
"""

_DESCRIBE_TABLE_SQL = """
    SELECT column_name, data_type, is_nullable
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = $1
    ORDER BY ordinal_position
"""


@mcp.tool(auth=require_scopes("tier:pro"), annotations=_READONLY_ANNOTATIONS)
async def describe_table(table_name: str | None = None) -> str:
    """自省 readonly SQL 的 schema —— `execute_readonly_sql` 下手前先看欄位,避免猜錯欄位名。

    需 pro tier(跟 `execute_readonly_sql` 同一組)。

    Args:
        table_name: 想看的 table 名(例 "companies");省略或留空 = 列出所有可查的 table。

    Returns:
        JSON 字串。
          - 不帶 table_name:{"tables": ["balance_sheets", "companies", ...]}
          - 帶 table_name:{"table": "companies", "columns": [
                {"column_name": "ticker", "data_type": "text", "is_nullable": "NO"}, ...]}
            查無此表(回空欄位)→ {"error": "..."}。
    """
    try:
        pool = await get_pool()
    except ReadonlyDBNotConfigured as exc:
        return json.dumps({"error": str(exc)})

    try:
        async with pool.acquire() as conn:
            if table_name is None or not table_name.strip():
                records = await conn.fetch(_LIST_TABLES_SQL)
                return json.dumps({"tables": [r["table_name"] for r in records]})

            records = await conn.fetch(_DESCRIBE_TABLE_SQL, table_name.strip())
    except Exception as exc:  # asyncpg.PostgresError 等 —— surface 給 LLM。
        return json.dumps({"error": f"Schema lookup failed: {exc}"})

    if not records:
        return json.dumps(
            {"error": f"No table named '{table_name}' in public schema; "
                      "call describe_table() with no argument to list tables."}
        )
    return json.dumps({"table": table_name.strip(), "columns": [dict(r) for r in records]})
