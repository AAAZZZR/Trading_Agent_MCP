"""`execute_readonly_sql` tool 的整合測試 —— mock asyncpg pool,不真連 DB。

涵蓋:
  - execute_readonly_sql / describe_table 註冊性
  - SELECT 成功路徑(回 JSON,row_count 對)
  - validation 拒絕(DELETE / multi-statement / pg_sleep)→ 回 friendly error
  - LIMIT 自動加 / clamp(外層硬性上界)真的傳到 DB
  - DSN 沒設 → 回 ReadonlyDBNotConfigured 的 friendly error
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trading_agent_mcp import db as db_module
from trading_agent_mcp import tools
from trading_agent_mcp.server import mcp
from trading_agent_mcp.settings import settings

# ---- 註冊性 --------------------------------------------------------------


async def test_execute_readonly_sql_registered() -> None:
    # 用 provider 層的 list_tools(未經 auth 過濾)驗「註冊」這件事 ——
    # mcp.list_tools() 會做 tier gating,無 auth context 時會把此 tool 藏起來。
    registered = await mcp._local_provider.list_tools()
    names = {t.name for t in registered}
    assert "execute_readonly_sql" in names
    assert "describe_table" in names


# ---- helper:把 fake pool 塞進 db._pool -----------------------------------


def _make_fake_pool(records: list[dict]):
    """產生一個 fake asyncpg pool。

    - pool.acquire() 是 async context manager,yield 一個 connection。
    - conn.fetch(query) 回傳 list of records;紀錄 query 到 .last_query。
    """
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[_FakeRecord(r) for r in records])

    @asynccontextmanager
    async def acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = acquire
    return pool, conn


class _FakeRecord(dict):
    """模擬 asyncpg.Record —— `dict(record)` 即可拿到 mapping。
    asyncpg 真實 Record 行為比 dict 多,但 tool 只用 `dict(r)`,夠用。
    """


@pytest.fixture
def patched_pool():
    """每個測試前後重設 db._pool,並提供 helper 注入 fake pool。"""
    db_module._pool = None
    yield
    db_module._pool = None


# ---- 成功路徑 ------------------------------------------------------------


async def test_select_returns_json(patched_pool) -> None:
    pool, conn = _make_fake_pool([{"ticker": "AAPL", "name": "Apple"}])

    with patch.object(tools, "get_pool", AsyncMock(return_value=pool)):
        result = await tools.execute_readonly_sql(
            query="SELECT ticker, name FROM companies LIMIT 1"
        )

    payload = json.loads(result)
    assert payload["row_count"] == 1
    assert payload["rows"] == [{"ticker": "AAPL", "name": "Apple"}]
    # conn.fetch 應該被呼叫,且 query 帶 LIMIT 1(原樣,合理範圍不變)
    conn.fetch.assert_awaited_once()
    sent_query = conn.fetch.call_args.args[0]
    assert "LIMIT 1" in sent_query


class _DupColRecord:
    """模擬帶重複欄名的 asyncpg.Record —— 真實 Record.items() 會逐欄位 yield(含同名)。

    plain dict 塞不下同名 key,所以另建這個只實作 .items() 的極簡替身。
    """

    def __init__(self, pairs: list[tuple[str, object]]) -> None:
        self._pairs = pairs

    def items(self):
        return iter(self._pairs)


def _make_fake_pool_with_records(records: list):
    """跟 _make_fake_pool 一樣,但直接吃 record 物件(不強制轉 _FakeRecord)。"""
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=records)

    @asynccontextmanager
    async def acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = acquire
    return pool, conn


async def test_duplicate_column_names_preserved_with_suffix(patched_pool) -> None:
    """JOIN 出同名欄 → 不互蓋,第二個以後加 _2 / _3 後綴保留全部欄位。"""
    dup = _DupColRecord([("id", 1), ("id", 2), ("id", 3), ("name", "AAPL")])
    pool, _ = _make_fake_pool_with_records([dup])

    with patch.object(tools, "get_pool", AsyncMock(return_value=pool)):
        result = await tools.execute_readonly_sql(
            query="SELECT a.id, b.id, c.id, a.name FROM a JOIN b USING (x) JOIN c USING (y)"
        )

    payload = json.loads(result)
    assert payload["rows"] == [{"id": 1, "id_2": 2, "id_3": 3, "name": "AAPL"}]


async def test_select_serializes_date_and_decimal(patched_pool) -> None:
    """date / Decimal 要能 JSON serialize(透過 _json_default)。"""
    pool, _ = _make_fake_pool(
        [{"filed_at": date(2024, 1, 1), "revenue": Decimal("123.45")}]
    )

    with patch.object(tools, "get_pool", AsyncMock(return_value=pool)):
        result = await tools.execute_readonly_sql(query="SELECT 1")

    payload = json.loads(result)
    assert payload["rows"][0]["filed_at"] == "2024-01-01"
    assert payload["rows"][0]["revenue"] == "123.45"


# ---- LIMIT 行為傳到 DB --------------------------------------------------


async def test_missing_limit_added(patched_pool) -> None:
    pool, conn = _make_fake_pool([])

    with patch.object(tools, "get_pool", AsyncMock(return_value=pool)):
        await tools.execute_readonly_sql(query="SELECT * FROM companies")

    sent_query = conn.fetch.call_args.args[0]
    assert "LIMIT 1000" in sent_query


async def test_oversized_limit_clamped(patched_pool) -> None:
    pool, conn = _make_fake_pool([])

    with patch.object(tools, "get_pool", AsyncMock(return_value=pool)):
        await tools.execute_readonly_sql(query="SELECT * FROM companies LIMIT 999999")

    # 改寫成「外層硬性上界」後:原始 LIMIT 999999 保留在子查詢內,但外層補 LIMIT 10000
    # 收斂(實際回不到 999999 列),這正是內層 LIMIT 不可繞過的保證。
    sent_query = conn.fetch.call_args.args[0]
    assert sent_query.rstrip().endswith("LIMIT 10000")


# ---- 拒絕路徑(validation 擋下,DB 不被呼叫) -----------------------------


@pytest.mark.parametrize(
    "bad_sql",
    [
        "DELETE FROM companies",
        "INSERT INTO companies VALUES ('X')",
        "UPDATE companies SET name='X'",
        "DROP TABLE companies",
        "CREATE TABLE x(id int)",
        "ALTER TABLE companies ADD COLUMN x int",
        "TRUNCATE companies",
        "SELECT 1; DELETE FROM companies",
        "SELECT pg_sleep(10)",
        "COPY companies TO '/tmp/x'",
        "",
    ],
)
async def test_rejected_queries_return_error_without_db_call(
    patched_pool, bad_sql: str
) -> None:
    pool, conn = _make_fake_pool([])

    with patch.object(tools, "get_pool", AsyncMock(return_value=pool)):
        result = await tools.execute_readonly_sql(query=bad_sql)

    payload = json.loads(result)
    assert "error" in payload
    conn.fetch.assert_not_awaited()


# ---- DSN 沒設 → friendly error -----------------------------------------


async def test_no_dsn_returns_friendly_error(patched_pool) -> None:
    """用真的 get_pool,驗「DSN 沒設 → friendly error」而不是炸給 LLM。

    這裡**必須**明確把 settings.mcp_readonly_db_dsn 壓成 ""。conftest 已經清掉
    `MCP_READONLY_DB_DSN` 環境變數,但 settings 是 import 期就實例化的 module-level
    單例、又會讀 `.env` 檔 —— 少了這層 patch,只要跑測試的機器上有真的 DSN,這個測試
    就會繞過 mock 去連真的 Postgres(慢、外部相依、還可能打到 prod)。
    """
    with patch.object(settings, "mcp_readonly_db_dsn", ""):
        result = await tools.execute_readonly_sql(query="SELECT 1")
    payload = json.loads(result)
    assert "error" in payload
    assert "MCP_READONLY_DB_DSN" in payload["error"]


# ---- DB 端錯誤 surface 給 LLM ------------------------------------------


async def test_db_error_returned_as_friendly_error(patched_pool) -> None:
    pool, conn = _make_fake_pool([])
    conn.fetch = AsyncMock(side_effect=RuntimeError("syntax error at or near 'foo'"))

    with patch.object(tools, "get_pool", AsyncMock(return_value=pool)):
        result = await tools.execute_readonly_sql(query="SELECT foo FROM bar")

    payload = json.loads(result)
    assert "error" in payload
    assert "syntax error" in payload["error"]


# ---- describe_table:schema 自省 ----------------------------------------


async def test_describe_table_lists_tables_when_no_arg(patched_pool) -> None:
    """不帶 table_name → 回所有 table 名(查 information_schema.tables)。"""
    pool, conn = _make_fake_pool(
        [{"table_name": "companies"}, {"table_name": "prices_hourly"}]
    )

    with patch.object(tools, "get_pool", AsyncMock(return_value=pool)):
        result = await tools.describe_table()

    payload = json.loads(result)
    assert payload["tables"] == ["companies", "prices_hourly"]
    # 沒有帶 bind param(列 table 用無參數 query)
    conn.fetch.assert_awaited_once()
    assert len(conn.fetch.call_args.args) == 1  # 只有 SQL,沒有 $1


async def test_describe_table_returns_columns(patched_pool) -> None:
    """帶 table_name → 回欄位 + 型別,且 table 名以 bind param($1)傳入。"""
    pool, conn = _make_fake_pool(
        [
            {"column_name": "ticker", "data_type": "text", "is_nullable": "NO"},
            {"column_name": "cik", "data_type": "text", "is_nullable": "YES"},
        ]
    )

    with patch.object(tools, "get_pool", AsyncMock(return_value=pool)):
        result = await tools.describe_table(table_name="companies")

    payload = json.loads(result)
    assert payload["table"] == "companies"
    assert payload["columns"][0]["column_name"] == "ticker"
    # table 名走 asyncpg bind param,不拼進 SQL 字串(防注入)
    sent_args = conn.fetch.call_args.args
    assert sent_args[1] == "companies"


async def test_describe_table_unknown_table_returns_error(patched_pool) -> None:
    """查無此表(空欄位)→ friendly error,提示用 describe_table() 列表。"""
    pool, _ = _make_fake_pool([])

    with patch.object(tools, "get_pool", AsyncMock(return_value=pool)):
        result = await tools.describe_table(table_name="does_not_exist")

    payload = json.loads(result)
    assert "error" in payload
    assert "does_not_exist" in payload["error"]


async def test_describe_table_no_dsn_returns_friendly_error(patched_pool) -> None:
    """DSN 沒設 → friendly error(跟 execute_readonly_sql 一致)。

    同樣顯式 patch settings,理由見 `test_no_dsn_returns_friendly_error`。
    """
    with patch.object(settings, "mcp_readonly_db_dsn", ""):
        result = await tools.describe_table(table_name="companies")
    payload = json.loads(result)
    assert "error" in payload
    assert "MCP_READONLY_DB_DSN" in payload["error"]
