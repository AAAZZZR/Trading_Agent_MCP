"""`execute_readonly_sql` tool 的整合測試 —— mock asyncpg pool,不真連 DB。

涵蓋:
  - 16 tool 註冊性(取代 15 個的測試)
  - SELECT 成功路徑(回 JSON,row_count 對)
  - validation 拒絕(DELETE / multi-statement / pg_sleep)→ 回 friendly error
  - LIMIT 自動加 / clamp 真的傳到 DB
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

# ---- 註冊性 --------------------------------------------------------------


async def test_execute_readonly_sql_registered() -> None:
    # 用 provider 層的 list_tools(未經 auth 過濾)驗「註冊」這件事 ——
    # mcp.list_tools() 會做 tier gating,無 auth context 時會把此 tool 藏起來。
    registered = await mcp._local_provider.list_tools()
    names = {t.name for t in registered}
    assert "execute_readonly_sql" in names


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

    sent_query = conn.fetch.call_args.args[0]
    assert "LIMIT 10000" in sent_query
    assert "999999" not in sent_query


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
    # 用真的 get_pool;settings.mcp_readonly_db_dsn 預設為 ""(conftest 沒注入)
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
