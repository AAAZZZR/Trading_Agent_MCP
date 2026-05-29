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
# Companies
# ============================================================


@mcp.tool
async def list_companies() -> list[dict[str, Any]]:
    """列出所有被追蹤的美股公司,依 ticker 字母排序。

    回傳整個 watchlist(可能 5000+ 筆,沒有分頁)。若只是要找特定公司,改用
    `search_companies`(吃名稱 / 模糊 ticker)會快很多;要跑統計或自訂篩選用
    `execute_readonly_sql` 查 `companies` 表。

    Returns:
        list[dict],每筆欄位:ticker, cik, name, sector, sic_code, industry,
        exchange, country(目前恆為 null), is_active(bool), first_seen(YYYY-MM-DD),
        last_updated(ISO datetime)。
    """
    return await api.get("/api/companies")


@mcp.tool
async def search_companies(q: str, limit: int = 20) -> list[dict[str, Any]]:
    """以 ticker 或公司名稱關鍵字模糊搜尋公司(typeahead 用),適合「我不知道精確 ticker」時。

    比對 ticker 前綴 / 子字串與名稱子字串(皆 case-insensitive),排序:ticker 完全相等 →
    ticker 前綴 → name 前綴 → 其餘子字串命中,同級短 ticker 優先。找到精確 ticker 後可再用
    `get_company` 取完整基本資料。

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

    Args:
        accession: SEC accession,例如 "0000320193-24-000123"。
    """
    return await api.get(f"/api/filings/{accession}")


@mcp.tool
async def list_filing_sections(accession: str) -> list[dict[str, Any]]:
    """列出某 filing 已解析的章節目錄(item_code / 標題 / 字元範圍,不含內文)。

    Args:
        accession: SEC accession。
    """
    return await api.get(f"/api/filings/{accession}/sections")


@mcp.tool
async def get_filing_section(accession: str, item_code: str) -> dict[str, Any]:
    """取得單一 filing 章節的完整內文。

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
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """取得每日 OHLC + 成交量(價格 USD),依日期升冪(最舊在前)。

    Args:
        ticker: 美股代號(自動轉大寫)。
        start: 起始日期(YYYY-MM-DD),包含。
        end: 結束日期(YYYY-MM-DD),包含。
        limit: 最多幾筆(1-5000,預設 1000)。

    Returns:
        list[dict],每筆含 date(YYYY-MM-DD)、open、high、low、close(USD)、volume。
        只要最新一筆用 `get_latest_price`;要 intraday 粒度用 `list_hourly_prices`。
    """
    params: dict[str, Any] = {"ticker": ticker.upper(), "limit": limit}
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    return await api.get("/api/prices", params=params)


@mcp.tool
async def get_latest_price(ticker: str) -> dict[str, Any]:
    """取得最新一個交易日的 OHLC + 成交量(價格 USD)。

    Args:
        ticker: 美股代號(自動轉大寫)。

    Returns:
        dict,含 date(YYYY-MM-DD)、open、high、low、close(USD)、volume。
        查無價格 → tool error(後端 404)。
    """
    return await api.get("/api/prices/latest", params={"ticker": ticker.upper()})


@mcp.tool
async def list_hourly_prices(
    ticker: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    """取得每小時 OHLC + 成交量(dt 升冪,timestamptz)。

    粒度比 `list_daily_prices` 細,適合做 intraday 分析或 backtest 對齊。
    PROD 2026-05 現況:資料 worker 還沒部署,可能回空 list。

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
# Holdings(yfinance 資料源)
# ============================================================


@mcp.tool
async def list_institutional_holders(ticker: str) -> list[dict[str, Any]]:
    """取得主要機構持股清單(第三方聚合,來自 yfinance / Yahoo Finance,通常只有前 10 大)。

    這是即時但「不完整」的 Yahoo 快照。要第一手、完整、可分析季度變動的 SEC 13F 資料,
    改用 `list_13f_holders`(by stock)/ `list_13f_portfolio`(by filer)。

    Args:
        ticker: 美股代號(自動轉大寫)。

    Returns:
        list[dict],每筆含 holder、date_reported、shares、value(USD)、
        pct_held、pct_change。
    """
    return await api.get("/api/holdings/institutions", params={"ticker": ticker.upper()})


@mcp.tool
async def get_holders_breakdown(ticker: str) -> dict[str, Any]:
    """取得內部人 / 機構持股比例總覽(第三方聚合,來自 yfinance / Yahoo)。

    Args:
        ticker: 美股代號(自動轉大寫)。

    Returns:
        dict,含 insiders_pct、institutions_pct、institutions_float_pct、
        institutions_count、source。比例為 0-1 的小數。
    """
    return await api.get("/api/holdings/major", params={"ticker": ticker.upper()})


# ============================================================
# 13F-HR(第一手 SEC 機構持股 —— 比 /holdings yfinance 完整)
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

    可查的 14 張 table:companies, institutions, institution_filings, filings, filing_sections,
    income_statements, balance_sheets, cash_flow_statements, insider_trades,
    institutional_holdings, prices_hourly, financials_quarantine, ingest_runs, alembic_version。

    安全保證(三層):
      1. SQL parsing:只允許單一 SELECT / WITH ... SELECT,拒絕 INSERT/UPDATE/DELETE/DROP 等
         與 pg_sleep / copy / lo_import 等敏感函式。
      2. DB role:連線使用 `investor_db_readonly` role(僅 SELECT 權限)。
      3. Statement timeout:每段查詢 5 秒上限,複雜 query 自動 abort。

    LIMIT 自動處理:
      - 沒寫 → 自動加 LIMIT 1000。
      - 寫了 > 10000 → clamp 成 10000。
      - 寫了 <= 10000 → 不動。

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
