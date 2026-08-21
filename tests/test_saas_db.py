"""SaaS 控制面 DB 存取層的單元測試。

不連真 DB —— 用假的 pool 攔下 asyncpg 呼叫,驗的是「送出去的 SQL 與參數對不對」
以及 pool 生命週期(lazy 建、失敗要清乾淨、close 後可重建)。SQL 語意本身
(COALESCE 落 free、滾動 24 小時視窗、last_used_at 節流)是跨 repo 的契約,
釘在這裡才不會被無聲改掉。
"""

from unittest.mock import AsyncMock

import pytest

from trading_agent_mcp import saas_db
from trading_agent_mcp.saas_db import (
    DEFAULT_TIER,
    LAST_USED_THROTTLE,
    QUERY_TIMEOUT_S,
    SaasDBNotConfigured,
)

_SAAS_DSN = "postgresql://saas:pw@test-db:5432/zeabur"


class _FakeConn:
    """記下每次呼叫的 (sql, args, timeout),回傳預先設定的值。"""

    def __init__(self, result=None) -> None:
        self.result = result
        self.calls: list[tuple[str, tuple, float | None]] = []

    # timeout 用 **kwargs 收(而不是具名參數):asyncpg 的簽名就是 timeout=,
    # 但在 async def 上具名 timeout 會被 ruff ASYNC109 擋(那條規則是針對「自己
    # 實作逾時」的 API,不適用於這種假造第三方簽名的 stub)。
    async def fetchrow(self, sql, *args, **kwargs):
        self.calls.append((sql, args, kwargs.get("timeout")))
        return self.result

    async def fetchval(self, sql, *args, **kwargs):
        self.calls.append((sql, args, kwargs.get("timeout")))
        return self.result

    async def execute(self, sql, *args, **kwargs):
        self.calls.append((sql, args, kwargs.get("timeout")))
        return "UPDATE 1"


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self.conn

    async def __aexit__(self, *exc) -> None:
        return None


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn
        self.acquire_timeouts: list[float | None] = []
        self.closed = False

    def acquire(self, timeout=None) -> _FakeAcquire:
        self.acquire_timeouts.append(timeout)
        return _FakeAcquire(self.conn)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_pool():
    """module-level singleton 會跨測試殘留,每個測試前後都清掉。"""
    saas_db._pool = None
    yield
    saas_db._pool = None


def _install_pool(monkeypatch, conn: _FakeConn) -> _FakePool:
    pool = _FakePool(conn)
    monkeypatch.setattr(saas_db, "_pool", pool)
    monkeypatch.setattr(saas_db.settings, "mcp_saas_database_url", _SAAS_DSN)
    return pool


# ============================================================
# pool 生命週期
# ============================================================


async def test_get_pool_without_dsn_raises() -> None:
    """沒設 DSN → SaasDBNotConfigured(不是 import 期就炸)。"""
    with pytest.raises(SaasDBNotConfigured):
        await saas_db.get_pool()


async def test_get_pool_creates_once(monkeypatch) -> None:
    """lazy 建一次,之後重用同一個 pool。"""
    monkeypatch.setattr(saas_db.settings, "mcp_saas_database_url", _SAAS_DSN)
    pool = _FakePool(_FakeConn())
    create = AsyncMock(return_value=pool)
    monkeypatch.setattr(saas_db.asyncpg, "create_pool", create)

    assert await saas_db.get_pool() is pool
    assert await saas_db.get_pool() is pool
    assert create.await_count == 1
    # 連線與 query 都有時間上限,DB 卡住不會拖著 MCP 請求一起卡。
    kwargs = create.await_args.kwargs
    assert kwargs["command_timeout"] == QUERY_TIMEOUT_S
    assert kwargs["timeout"] == saas_db.POOL_CONNECT_TIMEOUT_S


async def test_failed_pool_creation_is_not_cached(monkeypatch) -> None:
    """建 pool 失敗不能把半殘狀態留在 singleton —— 下次要能重試。"""
    monkeypatch.setattr(saas_db.settings, "mcp_saas_database_url", _SAAS_DSN)
    create = AsyncMock(side_effect=OSError("db down"))
    monkeypatch.setattr(saas_db.asyncpg, "create_pool", create)

    with pytest.raises(OSError):
        await saas_db.get_pool()
    assert saas_db._pool is None

    # DB 復原 → 第二次要真的重建。
    pool = _FakePool(_FakeConn())
    monkeypatch.setattr(saas_db.asyncpg, "create_pool", AsyncMock(return_value=pool))
    assert await saas_db.get_pool() is pool


async def test_close_pool_resets_singleton(monkeypatch) -> None:
    pool = _install_pool(monkeypatch, _FakeConn())
    await saas_db.close_pool()
    assert pool.closed is True
    assert saas_db._pool is None


# ============================================================
# lookup_key
# ============================================================


async def test_lookup_key_binds_hash_and_default_tier(monkeypatch) -> None:
    """用 key_hash(有 UNIQUE index)查,tier 用 LEFT JOIN + COALESCE 一次帶回。"""
    conn = _FakeConn(result={"api_key_id": "k", "user_id": "u", "revoked_at": None})
    pool = _install_pool(monkeypatch, conn)

    row = await saas_db.lookup_key("deadbeef")

    assert row == {"api_key_id": "k", "user_id": "u", "revoked_at": None}
    sql, args, timeout = conn.calls[0]
    assert args == ("deadbeef", DEFAULT_TIER)
    assert timeout == QUERY_TIMEOUT_S
    assert pool.acquire_timeouts == [QUERY_TIMEOUT_S]
    assert "WHERE k.key_hash = $1" in sql
    # 沒有 subscriptions 列 = free —— 預設值當參數傳,不寫死在 SQL 字面值。
    assert "LEFT JOIN subscriptions" in sql
    assert "COALESCE(s.tier, $2)" in sql
    assert "revoked_at" in sql


async def test_lookup_key_returns_none_for_unknown_hash(monkeypatch) -> None:
    conn = _FakeConn(result=None)
    _install_pool(monkeypatch, conn)
    assert await saas_db.lookup_key("nope") is None


# ============================================================
# count_usage_24h
# ============================================================


async def test_count_usage_uses_rolling_24h_window(monkeypatch) -> None:
    """滾動 24 小時 + surface='mcp' —— 與 API repo 的配額 SQL 同一口徑。"""
    conn = _FakeConn(result=42)
    _install_pool(monkeypatch, conn)

    assert await saas_db.count_usage_24h("user-1") == 42

    sql, args, timeout = conn.calls[0]
    assert args == ("user-1",)
    assert timeout == QUERY_TIMEOUT_S
    assert "now() - interval '24 hours'" in sql
    assert "surface = 'mcp'" in sql
    assert "$1::uuid" in sql


# ============================================================
# record_usage
# ============================================================


async def test_record_usage_writes_event_and_throttles_last_used(monkeypatch) -> None:
    """一個 statement 做兩件事:記一筆 usage_events + 節流更新 last_used_at。"""
    conn = _FakeConn()
    _install_pool(monkeypatch, conn)

    await saas_db.record_usage(user_id="u1", api_key_id="k1", action="get_company")

    assert len(conn.calls) == 1, "兩件事應併成一次 round-trip"
    sql, args, timeout = conn.calls[0]
    assert args == ("u1", "k1", "get_company")
    assert timeout == QUERY_TIMEOUT_S
    assert "INSERT INTO usage_events" in sql
    assert "'mcp'" in sql
    assert "UPDATE api_keys" in sql
    # 節流:NULL 或超過門檻才寫,避免每次呼叫都去鎖同一列。
    assert "last_used_at IS NULL" in sql
    assert f"interval '{LAST_USED_THROTTLE}'" in sql
