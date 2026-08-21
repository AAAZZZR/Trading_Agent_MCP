"""UsageMiddleware(元件層計量 + 超額攔截)的單元測試。

計量是 fire-and-forget —— middleware 不 await 那個 task,所以測試要自己等它跑完
再斷言(見 `_flush_metering`)。
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from fastmcp.server.middleware import MiddlewareContext
from mcp.types import CallToolRequestParams

from trading_agent_mcp.usage import QUOTA_EXCEEDED_MESSAGE, UsageMiddleware, _pending_writes

_SENTINEL = object()


def _context(tool_name: str = "get_company") -> MiddlewareContext:
    return MiddlewareContext(
        message=CallToolRequestParams(name=tool_name, arguments={"ticker": "AAPL"})
    )


def _token(**claims) -> AccessToken:
    base = {
        "tier": "free",
        "user_id": "user-1",
        "api_key_id": "key-1",
        "quota_exceeded": False,
        "used_today": 1,
        "daily_limit": 200,
    }
    base.update(claims)
    return AccessToken(token="t", client_id=base["user_id"], scopes=["tier:free"], claims=base)


def _patch(monkeypatch, token: AccessToken | None, record: AsyncMock) -> None:
    monkeypatch.setattr("trading_agent_mcp.usage.get_access_token", lambda: token)
    monkeypatch.setattr("trading_agent_mcp.saas_db.record_usage", record)


async def _flush_metering() -> None:
    """等背景計量 task 跑完。正式路徑刻意不等它(計量不該擋著 tool),
    所以測試得手動把 event loop 讓出去幾輪。"""
    for _ in range(5):
        pending = list(_pending_writes)
        if not pending:
            return
        await asyncio.gather(*pending)
        await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def _clear_pending():
    """module-level 的 task set 跨測試共用,先清乾淨避免互相干擾。"""
    _pending_writes.clear()
    yield
    _pending_writes.clear()


async def test_records_usage_and_calls_tool(monkeypatch) -> None:
    """正常路徑:tool 照跑,並以 (user_id, api_key_id, tool 名) 記一筆。"""
    record = AsyncMock()
    _patch(monkeypatch, _token(), record)
    call_next = AsyncMock(return_value=_SENTINEL)

    result = await UsageMiddleware().on_call_tool(_context("get_financials"), call_next)
    await _flush_metering()

    assert result is _SENTINEL
    call_next.assert_awaited_once()
    record.assert_awaited_once_with(user_id="user-1", api_key_id="key-1", action="get_financials")


async def test_quota_exceeded_blocks_tool(monkeypatch) -> None:
    """超額 → ToolError,tool 不執行、也不記量(它根本沒發生)。"""
    record = AsyncMock()
    _patch(monkeypatch, _token(quota_exceeded=True), record)
    call_next = AsyncMock(return_value=_SENTINEL)

    with pytest.raises(ToolError) as exc:
        await UsageMiddleware().on_call_tool(_context(), call_next)
    await _flush_metering()

    assert str(exc.value) == QUOTA_EXCEEDED_MESSAGE
    call_next.assert_not_awaited()
    record.assert_not_awaited()


def test_quota_message_does_not_blame_the_key() -> None:
    """文案不能讓 agent 建議使用者重連 / 重建 key —— 那正是舊行為的病灶。
    也不能講成「明天重置」:視窗是滾動 24 小時。"""
    assert "still" in QUOTA_EXCEEDED_MESSAGE and "valid" in QUOTA_EXCEEDED_MESSAGE
    assert "rolling 24 hours" in QUOTA_EXCEEDED_MESSAGE
    assert "tomorrow" not in QUOTA_EXCEEDED_MESSAGE.lower()


async def test_metering_failure_does_not_break_tool(monkeypatch) -> None:
    """計量寫失敗 → 只進 log,tool 結果照常回傳,例外不往外冒。"""
    record = AsyncMock(side_effect=RuntimeError("db down"))
    _patch(monkeypatch, _token(), record)
    call_next = AsyncMock(return_value=_SENTINEL)

    result = await UsageMiddleware().on_call_tool(_context(), call_next)
    await _flush_metering()

    assert result is _SENTINEL
    record.assert_awaited_once()


async def test_no_access_token_skips_metering(monkeypatch) -> None:
    """stdio / 無 auth(get_access_token 回 None)→ 直接放行,不計量。"""
    record = AsyncMock()
    _patch(monkeypatch, None, record)
    call_next = AsyncMock(return_value=_SENTINEL)

    result = await UsageMiddleware().on_call_tool(_context(), call_next)
    await _flush_metering()

    assert result is _SENTINEL
    call_next.assert_awaited_once()
    record.assert_not_awaited()


async def test_oauth_session_without_api_key_is_still_metered(monkeypatch) -> None:
    """OAuth session 沒有 api_key_id → 仍然計量(api_key_id 傳 None)。

    漏掉這條等於白送 OAuth 使用者無限額度:額度是靠數 usage_events 算的。
    """
    record = AsyncMock()
    _patch(monkeypatch, _token(api_key_id=None, user_id="user-oauth"), record)
    call_next = AsyncMock(return_value=_SENTINEL)

    result = await UsageMiddleware().on_call_tool(_context("get_prices"), call_next)
    await _flush_metering()

    assert result is _SENTINEL
    record.assert_awaited_once_with(user_id="user-oauth", api_key_id=None, action="get_prices")


async def test_token_without_user_claims_skips_metering(monkeypatch) -> None:
    """static token 模式沒有 user_id → 放行但不記量
    (client_id 不是 uuid,硬記只會每次 INSERT 都失敗刷 log)。"""
    record = AsyncMock()
    static_token = AccessToken(
        token="shared", client_id="investor-db-default", scopes=["tier:pro"], claims={}
    )
    _patch(monkeypatch, static_token, record)
    call_next = AsyncMock(return_value=_SENTINEL)

    result = await UsageMiddleware().on_call_tool(_context(), call_next)
    await _flush_metering()

    assert result is _SENTINEL
    call_next.assert_awaited_once()
    record.assert_not_awaited()


async def test_pending_task_is_kept_alive(monkeypatch) -> None:
    """送出的 task 要被強引用住 —— 否則 asyncio 可能在它跑完前就 GC 掉。"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_record(**_kwargs) -> None:
        started.set()
        await release.wait()

    _patch(monkeypatch, _token(), AsyncMock(side_effect=slow_record))
    await UsageMiddleware().on_call_tool(_context(), AsyncMock(return_value=_SENTINEL))

    await started.wait()
    assert len(_pending_writes) == 1
    release.set()
    await _flush_metering()
    assert not _pending_writes
