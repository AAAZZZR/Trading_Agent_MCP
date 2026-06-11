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
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

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

# ============================================================
# Meta / data coverage(資料新鮮度與覆蓋範圍 —— agent 信任基石)
# ============================================================


@mcp.tool
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
    non-US listings, EOD-or-slower, no analyst estimates.

    Data cadence: this report refreshed daily; per-domain freshness is in each domain entry.

    Returns:
        dict {as_of, notes, domains: [...]}. See field notes above.
    """
    return await api.get("/api/meta/coverage")


# ============================================================
# Companies
# ============================================================


@mcp.tool
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


@mcp.tool
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


@mcp.tool
async def get_company(ticker: str) -> dict[str, Any]:
    """以精確 ticker 取得單一公司的完整基本資料。不知道精確 ticker 時先用 `search_companies`。

    Data cadence: company registry refreshed daily ~06:00 UTC.

    Args:
        ticker: 美股代號,例如 AAPL、MSFT、NVDA(大小寫不拘,自動轉大寫)。

    Returns:
        dict,欄位:ticker, cik, name, sector, sic_code, industry, exchange,
        country(目前恆為 null), is_active(bool), first_seen(YYYY-MM-DD),
        last_updated(ISO datetime)。查無此 ticker → tool error(後端 404)。
    """
    return await api.get(f"/api/companies/{ticker.upper()}")


# ============================================================
# Filings
# ============================================================


@mcp.tool
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


@mcp.tool
async def get_filing(accession: str) -> dict[str, Any]:
    """以 SEC accession number 取得單一 filing 的後設資料。

    Data cadence: discovered daily 06:00 UTC, parsed same day.

    Args:
        accession: SEC accession,例如 "0000320193-24-000123"。
    """
    return await api.get(f"/api/filings/{accession}")


@mcp.tool
async def list_filing_sections(accession: str) -> list[dict[str, Any]]:
    """列出某 filing 已解析的章節目錄(item_code / 標題 / 字元範圍,不含內文)。

    Data cadence: discovered daily 06:00 UTC, parsed same day.

    Args:
        accession: SEC accession。
    """
    return await api.get(f"/api/filings/{accession}/sections")


@mcp.tool
async def get_filing_section(accession: str, item_code: str) -> dict[str, Any]:
    """取得單一 filing 章節的完整內文。

    Data cadence: discovered daily 06:00 UTC, parsed same day.

    Args:
        accession: SEC accession。
        item_code: 章節編號,例如 10-K 的 "Item 1A"(風險因素)、"Item 7"(MD&A)。
    """
    return await api.get(f"/api/filings/{accession}/sections/{item_code}")


# ============================================================
# Financials
# ============================================================


# period 篩選:後端把 "annual" → fiscal_period='FY'、"quarterly" → IN ('Q1','Q2','Q3','Q4')。
# 回傳列本身帶 fiscal_year / fiscal_period 欄位,agent 可據此辨識是哪一期。
Period = Literal["annual", "quarterly"]


@mcp.tool
async def get_income_statements(
    ticker: str,
    period: Period | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """取得損益表(營收、毛利、營業利益、淨利、EPS 等,金額 USD),依期末由新到舊。

    Data cadence: from SEC XBRL filings, refreshed daily 07:00 UTC for recent filers; USD-normalized.

    Args:
        ticker: 美股代號(自動轉大寫)。
        period: "annual"(年報 FY)或 "quarterly"(季報 Q1-Q4);省略 = 兩者都回。
        limit: 最多回傳幾筆(1-200,預設 20)。

    Returns:
        list[dict],每筆含 fiscal_year、fiscal_period、period_end(YYYY-MM-DD)與各項
        財務數字(USD)。要一次拿三表合體用 `get_latest_period`。
    """
    params: dict[str, Any] = {"ticker": ticker.upper(), "limit": limit}
    if period is not None:
        params["period"] = period
    return await api.get("/api/financials/income", params=params)


@mcp.tool
async def get_balance_sheets(
    ticker: str,
    period: Period | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """取得資產負債表(現金、應收、存貨、PPE、總資產、總負債、權益等,金額 USD),依期末由新到舊。

    Data cadence: from SEC XBRL filings, refreshed daily 07:00 UTC for recent filers; USD-normalized.

    Args:
        ticker: 美股代號(自動轉大寫)。
        period: "annual"(FY)或 "quarterly"(Q1-Q4);省略 = 兩者都回。
        limit: 最多回傳幾筆(1-200,預設 20)。

    Returns:
        list[dict],每筆含 fiscal_year、fiscal_period、period_end(YYYY-MM-DD)與各項
        資產負債科目(USD)。
    """
    params: dict[str, Any] = {"ticker": ticker.upper(), "limit": limit}
    if period is not None:
        params["period"] = period
    return await api.get("/api/financials/balance", params=params)


@mcp.tool
async def get_cash_flow_statements(
    ticker: str,
    period: Period | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """取得現金流量表(營業現金流、capex、自由現金流、股利、回購等,金額 USD),依期末由新到舊。

    Data cadence: from SEC XBRL filings, refreshed daily 07:00 UTC for recent filers; USD-normalized.

    Args:
        ticker: 美股代號(自動轉大寫)。
        period: "annual"(FY)或 "quarterly"(Q1-Q4);省略 = 兩者都回。
        limit: 最多回傳幾筆(1-200,預設 20)。

    Returns:
        list[dict],每筆含 fiscal_year、fiscal_period、period_end(YYYY-MM-DD)與各項
        現金流科目(USD)。
    """
    params: dict[str, Any] = {"ticker": ticker.upper(), "limit": limit}
    if period is not None:
        params["period"] = period
    return await api.get("/api/financials/cashflow", params=params)


@mcp.tool
async def get_latest_period(
    ticker: str,
    period: Period | None = None,
) -> dict[str, Any]:
    """取得最近一期的三張財報合體(income + balance + cash_flow,同一個 period_end,金額 USD)。

    Data cadence: from SEC XBRL filings, refreshed daily 07:00 UTC for recent filers; USD-normalized.

    Args:
        ticker: 美股代號(自動轉大寫)。
        period: "annual" 取最近一個年報期;"quarterly" 取最近一季;省略 = 不限期別取最新。

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


@mcp.tool
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


@mcp.tool
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
    `list_hourly_prices`。完整歷史 pull 可能不小,只要近期請帶 start/end 或縮小 limit。

    Data cadence: EOD T+1, refreshed Mon-Sat ~10:00 UTC; adjusted; 5-year rolling window; NOT real-time.

    Args:
        ticker: 美股代號(自動轉大寫)。
        start: 起始日期(YYYY-MM-DD),包含。省略 = 不設下界。
        end: 結束日期(YYYY-MM-DD),包含。省略 = 不設上界。
        limit: 最多幾筆(1-10000,預設 2000)。

    Returns:
        list[dict],每筆含 ticker、date(YYYY-MM-DD)、open、high、low、close(USD)、
        volume。**不含 adj_close**。只要最新一筆用 `get_latest_price`。查無資料回空 list。
    """
    params: dict[str, Any] = {"ticker": ticker.upper(), "limit": limit}
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    return await api.get("/api/prices/daily", params=params)


@mcp.tool
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


@mcp.tool
async def list_hourly_prices(
    ticker: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    """取得每小時 OHLC + 成交量(dt 升冪,timestamptz)。

    粒度比 `list_daily_prices` 細,適合做 intraday 分析或 backtest 對齊。
    歷史深度有限(60 天滾動保留),要長區間請改用 `list_daily_prices`。

    Data cadence: 60-minute bars, Mon-Fri ~22:00 UTC refresh; 60-day rolling retention.

    Args:
        ticker: 美股代號。
        start: 起始 UTC datetime(ISO,例 "2026-05-22T13:30:00Z"),包含。
        end: 結束 UTC datetime,包含。
        limit: 最多幾筆(1-20000,預設 5000)。
    """
    params: dict[str, Any] = {"ticker": ticker.upper(), "limit": limit}
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    return await api.get("/api/prices/hourly", params=params)


# ============================================================
# Holdings(第一手自算:13F + Form 4 + 流通股數)
# ============================================================


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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
        limit: 最多回傳幾筆(1-10000,預設 100)。

    Returns:
        list[dict],每筆含 filer_cik、filer_name、ticker、cusip、quarter_end(YYYY-MM-DD)、
        shares、market_value(USD)、change_in_shares、change_type(NEW/ADD/REDUCE/EXIT)。
    """
    params: dict[str, Any] = {"ticker": ticker.upper(), "limit": limit}
    if quarter_end is not None:
        params["quarter_end"] = quarter_end
    return await api.get("/api/13f/holders", params=params)


@mcp.tool
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
        limit: 最多回傳幾筆(1-10000,預設 100)。

    Returns:
        list[dict],欄位同 `list_13f_holders`(filer_cik、filer_name、ticker、cusip、
        quarter_end、shares、market_value(USD)、change_in_shares、change_type)。
        ticker 可能為 null(CUSIP 對不到 watchlist 的非美股 / 衍生品)。
    """
    params: dict[str, Any] = {"cik": cik, "limit": limit}
    if quarter_end is not None:
        params["quarter_end"] = quarter_end
    return await api.get("/api/13f/portfolio", params=params)


@mcp.tool
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
        limit: 最多回傳幾筆(1-10000,預設 50)。

    Returns:
        list[dict],欄位同 `list_13f_holders`,依 change_in_shares 遞減排序。
    """
    params: dict[str, Any] = {"ticker": ticker.upper(), "limit": limit}
    if quarter_end is not None:
        params["quarter_end"] = quarter_end
    return await api.get("/api/13f/top-buyers", params=params)


@mcp.tool
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
        limit: 最多回傳幾筆(1-10000,預設 50)。

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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
async def list_earnings(
    ticker: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """列出某公司(即將公布)的財報日清單,依公布日(report_date)由舊到新。

    給 swing trader 避開 earnings gap、安排進出場(單 ticker 視角)。要掃整個市場
    某區間誰公布財報改用 `get_earnings_calendar`。

    Data cadence: calendar refreshed daily 03:00 UTC; report dates are vendor estimates; NO analyst EPS estimates.

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


@mcp.tool
async def get_earnings_calendar(
    start: str | None = None,
    end: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """掃整個市場某日期區間內即將公布財報的公司(跨 ticker),依 (report_date, ticker) 由舊到新。

    回答「下週 / 某區間有哪些公司公布財報」。要單一公司的財報日改用 `list_earnings`。

    Data cadence: calendar refreshed daily 03:00 UTC; report dates are vendor estimates; NO analyst EPS estimates.

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
# ETF(概況 / 成分股 / 反查持有者,Alpha Vantage)
# ============================================================


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
async def get_macro_series(
    series_id: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    """取得某 series 的觀測時間序列,依日期升冪(最舊在前),邊界 inclusive。

    series_id 區分大小寫,請用 `list_macro_series` 目錄拿到的原值(例 CPI、
    TREASURY_YIELD_10YEAR、WTI、INDEX_VIX)。注意:**value 的單位不在本回應裡** ——
    要去 `list_macro_series` 查該 series 的 `unit` 欄位。

    Data cadence: economic/commodity series monthly; index series (SPX/NDX/VIX) daily (rollout in progress).

    Args:
        series_id: series 代碼(區分大小寫,來自 `list_macro_series`)。
        start: 起始日(含,YYYY-MM-DD);省略 = 不設下界。
        end: 結束日(含,YYYY-MM-DD);省略 = 不設上界。
        limit: 最多回傳幾筆(1-10000,預設 2000)。

    Returns:
        list[dict],每筆含 series_id、date(YYYY-MM-DD)、value(數值,單位見目錄的
        `unit`)。series_id 不存在或區間無資料回空 list。
    """
    params: dict[str, Any] = {"limit": limit}
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    return await api.get(f"/api/macro/series/{series_id}", params=params)


# ============================================================
# Screener(跨 ticker 篩選)
# ============================================================


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
async def get_overview(ticker: str) -> dict[str, Any]:
    """取得公司估值快照的原始數據:市值 / 本益比家族 / Beta / 52 週高低 / 均線 / 分析師目標價。

    純數據快照(貴賤、技術強弱由你判讀)。資料源 Alpha Vantage OVERVIEW(每日刷新)。要逐期
    財報數字用 get_income_statements 等;要四面向結論用 get_analysis。

    Data cadence: valuation snapshot self-computed daily 16:00 UTC from latest close x shares; market_cap covers ~7k of ~20k tickers — missing means not-covered, not zero.

    Args:
        ticker: 美股代號(自動轉大寫)。不知道精確 ticker 先用 search_companies。

    Returns:
        dict。金額(USD 整數,nullable):market_cap, shares_outstanding, ebitda, revenue_ttm,
        gross_profit_ttm。估值比率(float):pe_ratio, forward_pe, peg_ratio, price_to_book,
        price_to_sales_ttm, ev_to_ebitda, ev_to_revenue。每股:eps, diluted_eps_ttm, book_value。
        配息:dividend_per_share, dividend_yield(**0-1 小數**,非百分比)。獲利能力(皆 **0-1
        小數**):profit_margin, operating_margin_ttm, return_on_assets_ttm, return_on_equity_ttm。
        風險/技術:beta, week_52_high, week_52_low, ma_50, ma_200。analyst_target_price。
        latest_quarter(財報期末 YYYY-MM-DD)、updated_at(本表刷新時間 ISO)。多數 nullable。
        尚未被 overview ETL 覆蓋 → tool error(後端 404)。
    """
    return await api.get(f"/api/overview/{ticker.upper()}")


# ============================================================
# Options(期權 EOD —— options_eod)
# ============================================================


@mcp.tool
async def get_options_chain(
    underlying: str,
    as_of: str | None = None,
    expiration: str | None = None,
    option_type: Literal["call", "put"] | None = None,
    limit: int = 250,
) -> list[dict[str, Any]]:
    """取得某標的某交易日的期權鏈(EOD 報價 + IV + greeks),依 (到期日, 履約價, call/put) 排序。

    一列 = 一個 OCC 合約在該交易日的 EOD snapshot。**全鏈可達上千合約,預設 limit=250 防爆
    context**;建議先用 get_option_expirations 拿到期日,再帶 expiration 過濾。underlying 是
    AV symbol,不保證對得上 companies.ticker(指數選擇權、BRK.B/BRK-B 命名差異)。查無 →
    回空 list;若是符號寫法問題,試 dot/dash 兩種(BRK.B vs BRK-B)或先 search_companies。

    Data cadence: EOD chains for the prior trading day, fetched Mon-Fri 04:00 UTC.

    Args:
        underlying: 標的代號(AV symbol,自動轉大寫)。
        as_of: EOD 交易日(YYYY-MM-DD);省略 = 該標的最新交易日。
        expiration: 只取此到期日(YYYY-MM-DD);省略 = 全到期。
        option_type: "call" 或 "put";省略 = 兩者皆回。
        limit: 合約數上限(1-5000,預設 250)。

    Returns:
        list[dict],每筆含 contract_id(OCC)、date、underlying、expiration、strike、option_type、
        last、mark、bid、bid_size、ask、ask_size、volume、open_interest、implied_volatility、
        delta、gamma、theta、vega、rho。**數值欄(strike/報價/IV/各 greek)以 JSON 字串回傳
        (如 "200.0000"、"-0.019830"),做數學前先轉 float;greeks 可為負**;bid_size/ask_size/
        volume/open_interest 為整數。null = 報價/greek 缺失,勿當 0 納入計算。無資料回空 list。
    """
    params: dict[str, Any] = {"underlying": underlying.upper(), "limit": limit}
    if as_of is not None:
        params["as_of"] = as_of
    if expiration is not None:
        params["expiration"] = expiration
    if option_type is not None:
        params["option_type"] = option_type
    return await api.get("/api/options/chain", params=params)


@mcp.tool
async def get_option_expirations(
    underlying: str, as_of: str | None = None
) -> list[dict[str, Any]]:
    """列出某標的某交易日可選的到期日 + 各到期合約數,ascending expiration。

    給挑 expiration 用 —— 先拿到期日,再帶去 get_options_chain 過濾,避免一次拉整鏈。

    Data cadence: EOD chains for the prior trading day, fetched Mon-Fri 04:00 UTC.

    Args:
        underlying: 標的代號(AV symbol,自動轉大寫)。
        as_of: EOD 交易日(YYYY-MM-DD);省略 = 最新交易日。

    Returns:
        list[dict],每筆含 expiration(YYYY-MM-DD)、contract_count。無資料回空 list。
    """
    params: dict[str, Any] = {"underlying": underlying.upper()}
    if as_of is not None:
        params["as_of"] = as_of
    return await api.get("/api/options/expirations", params=params)


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


@mcp.tool
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


# Tier gating:execute_readonly_sql 是重量級 tool(自由 SQL),只開給 pro tier。
# require_scopes("tier:pro") 由 FastMCP 在元件層 enforce —— free tier(scopes 只有
# tier:free)在 list_tools 看不到此 tool,直接呼叫也會被擋(get_tool 回 None)。
# 其餘 structured tool 不加 auth,free / pro 都能用。
# 注意:auth 關閉(無 verifier)或 stdio 本機開發時框架會 skip_auth,此 gating 不生效。
@mcp.tool(auth=require_scopes("tier:pro"))
async def execute_readonly_sql(query: str) -> str:
    """跑一段 readonly SELECT,回 JSON 字串(rows + meta)。給需要彈性查詢的 agent / 分析用。

    需 pro tier(權限不足者看不到此 tool)。下 SQL 前不確定欄位?先用 `describe_table`
    自省(不帶參數 = 列出所有 table;帶 table 名 = 列出該表欄位 + 型別)。

    可查的 table:companies, institutions, institution_filings, filings, filing_sections,
    income_statements, balance_sheets, cash_flow_statements, insider_trades,
    institutional_holdings, prices_daily, prices_hourly, company_overview, options_eod,
    financials_quarantine, ingest_runs, alembic_version, dividends, splits,
    earnings_calendar, etf_profile, etf_holdings, macro_series, macro_series_meta。
    實際清單以 describe_table()(不帶參數)為準。

    安全保證(三層):
      1. SQL parsing:只允許單一 SELECT / WITH ... SELECT,拒絕 INSERT/UPDATE/DELETE/DROP 等
         與 pg_sleep / copy / lo_import 等敏感函式。
      2. DB role:連線使用 `investor_db_readonly` role(僅 SELECT 權限)。
      3. Statement timeout:每段查詢 5 秒上限,複雜 query 自動 abort。

    LIMIT 自動處理(把你的查詢包成 `SELECT * FROM (<你的 SQL>) _ LIMIT n` 加硬性外層上界):
      - 沒寫頂層 LIMIT → 外層補 LIMIT 1000。
      - 頂層 LIMIT N → 外層用 min(N, 10000)。
      - 子查詢內寫 LIMIT 也無法繞過(外層上界一定生效)。

    Output 超過 100KB 會截斷並附註記(改窄 WHERE / 縮小 LIMIT 再查)。

    Args:
        query: 要執行的 SELECT(單一 statement,不要加多個分號)。
               範例:`SELECT ticker, name FROM companies WHERE sector = 'Technology' LIMIT 50`

    Returns:
        JSON 字串,shape = {"row_count": N, "executed_query": "...", "rows": [...]}。
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

    rows = [dict(r) for r in records]
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


@mcp.tool(auth=require_scopes("tier:pro"))
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
