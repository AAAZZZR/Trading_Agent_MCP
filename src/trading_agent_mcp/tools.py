"""@mcp.tool —— 對應 Trading_Agent REST endpoint 的 16 個 tool。

前 15 個 per-domain pre-built(`list_companies` / `get_income_statements` 等)內部
就是一行 `await api.get(path, params=...)`,LLM 端透過 docstring 理解語意。

第 16 個 `execute_readonly_sql` 給 power user / AI agent 跑任意 SELECT,走獨立
readonly Postgres 連線(`db.py`),經 sqlparse + readonly role + statement timeout
三層防護。

之後如要強化 LLM 端 schema 推斷,可在本模組加 pydantic BaseModel 並
標記 tool 回傳型別 —— 目前以 dict / list[dict] 起步,先求覆蓋度。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

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
    """列出所有被追蹤的美股公司(ticker / cik / 公司名 / 行業 / 交易所等)。"""
    return await api.get("/api/companies")


@mcp.tool
async def get_company(ticker: str) -> dict[str, Any]:
    """以 ticker 取得單一公司的基本資料(cik、名稱、行業、SIC 代碼等)。

    Args:
        ticker: 美股代號,例如 AAPL、MSFT、NVDA(大小寫不拘)。
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
    """列出某公司提交過的 SEC filings,新到舊。

    Args:
        ticker: 美股代號。
        form_type: 表格類型過濾,例如 "10-K"(年報)、"10-Q"(季報)、"8-K"(臨時公告)、"4"(內部人交易);省略 = 全部。
        since: 起始 filed_at(ISO 日期 YYYY-MM-DD),包含。
        until: 結束 filed_at,包含。
        limit: 最多回傳幾筆(1-500,預設 50)。
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


Period = Literal["annual", "quarterly"]


@mcp.tool
async def get_income_statements(
    ticker: str,
    period: Period | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """取得損益表(營收、毛利、營業利益、淨利、EPS 等),新到舊。

    Args:
        ticker: 美股代號。
        period: "annual"(年報 FY)或 "quarterly"(Q1/Q2/Q3);省略 = 全部。
        limit: 最多回傳幾筆(1-200,預設 20)。
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
    """取得資產負債表(現金、應收、存貨、PPE、總資產、總負債、權益等),新到舊。

    Args:
        ticker: 美股代號。
        period: "annual" 或 "quarterly";省略 = 全部。
        limit: 最多回傳幾筆。
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
    """取得現金流量表(OCF、capex、FCF、股利、回購等),新到舊。

    Args:
        ticker: 美股代號。
        period: "annual" 或 "quarterly";省略 = 全部。
        limit: 最多回傳幾筆。
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
    """取得最近一期的三張財報合體(income + balance + cash_flow 同一個 period_end)。

    Args:
        ticker: 美股代號。
        period: "annual" 取最近一年;"quarterly" 取最近一季;省略 = 隨意。
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
    """列出某公司內部人(高管 / 董事 / 大股東)依 SEC Form 4 申報的交易紀錄。

    Args:
        ticker: 美股代號。
        since: 起始 transaction_date(YYYY-MM-DD)。
        until: 結束 transaction_date。
        limit: 最多回傳幾筆(1-1000,預設 100)。
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
    """取得每日 OHLC + 成交量(date 升冪)。

    Args:
        ticker: 美股代號。
        start: 起始日期(YYYY-MM-DD),包含。
        end: 結束日期,包含。
        limit: 最多幾筆(1-5000,預設 1000)。
    """
    params: dict[str, Any] = {"ticker": ticker.upper(), "limit": limit}
    if start is not None:
        params["start"] = start
    if end is not None:
        params["end"] = end
    return await api.get("/api/prices", params=params)


@mcp.tool
async def get_latest_price(ticker: str) -> dict[str, Any]:
    """取得最新一個交易日的 OHLC + 成交量。

    Args:
        ticker: 美股代號。
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
    """取得主要機構持股清單(來自 yfinance / Yahoo Finance,通常前 10 大)。

    Args:
        ticker: 美股代號。
    """
    return await api.get("/api/holdings/institutions", params={"ticker": ticker.upper()})


@mcp.tool
async def get_holders_breakdown(ticker: str) -> dict[str, Any]:
    """取得內部人 / 機構 / 散戶持股比例的總覽(來自 yfinance)。

    Args:
        ticker: 美股代號。
    """
    return await api.get("/api/holdings/major", params={"ticker": ticker.upper()})


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


@mcp.tool
async def execute_readonly_sql(query: str) -> str:
    """跑一段 readonly SELECT,回 JSON 字串(rows + meta)。給需要彈性查詢的 agent / 分析用。

    Schema 14 表:companies, institutions, institution_filings, filings, filing_sections,
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

    Output 超過 100KB 會截斷並附註記。

    Args:
        query: 要執行的 SELECT(單一 statement,不要加多個分號)。
               範例:`SELECT ticker, name FROM companies WHERE sector = 'Technology' LIMIT 50`
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
