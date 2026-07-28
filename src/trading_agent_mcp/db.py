"""`execute_readonly_sql` tool 的 asyncpg 連線池。

設計重點:

- Lazy init:第一次呼叫 tool 才建 pool;沒設 `MCP_READONLY_DB_DSN` 整個模組
  不會炸,只在 tool 被呼叫時回 friendly error。
- Module-level singleton:全 process 共用一個 pool,asyncpg 自己處理併發。
- 每條 connection 都跑 `SET statement_timeout` —— 用 setup callback 註冊,
  保證從 pool 拿到的都是被限時的 connection。
- 連線目標 role 應該是 `investor_db_readonly`(scripts/grant_readonly.sql),
  即使 sqlparse 漏網,role 也擋下 INSERT / UPDATE / DELETE / DDL。
"""

from __future__ import annotations

import asyncpg

from trading_agent_mcp.settings import settings

# Postgres statement timeout(每 query 上限),小到足以擋住失控 query,
# 大到允許正常 join + aggregation。5 秒對 14 表規模夠用。
STATEMENT_TIMEOUT_MS = 5000

# Connection pool 上限 —— MCP server 通常單機跑,給 LLM 連線量足。
_POOL_MIN_SIZE = 1
_POOL_MAX_SIZE = 5

_pool: asyncpg.Pool | None = None


class ReadonlyDBNotConfigured(RuntimeError):
    """`MCP_READONLY_DB_DSN` 沒設;tool 應該回 friendly error 而非 raise 給 LLM。"""


async def _setup_connection(conn: asyncpg.Connection) -> None:
    """每條新連線 session 級設 statement_timeout。

    用 ms 數字配 `SET LOCAL` 不行(LOCAL 只在 transaction 內有效),所以用
    plain SET —— pool 回收連線時 session 設定保留,正合我們需求。
    """
    await conn.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")


async def get_pool() -> asyncpg.Pool:
    """取(或第一次建)readonly pool。沒設 DSN → ReadonlyDBNotConfigured。"""
    global _pool
    if not settings.mcp_readonly_db_dsn:
        raise ReadonlyDBNotConfigured(
            "MCP_READONLY_DB_DSN is not set; execute_readonly_sql is disabled."
        )
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.mcp_readonly_db_dsn,
            min_size=_POOL_MIN_SIZE,
            max_size=_POOL_MAX_SIZE,
            setup=_setup_connection,
        )
    return _pool


async def close_pool() -> None:
    """測試 / shutdown 用 —— 關掉 pool 並重設 singleton。"""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
