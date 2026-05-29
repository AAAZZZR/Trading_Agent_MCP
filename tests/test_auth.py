"""server 端 auth 設定的單元測試。

涵蓋:
  - `_build_auth()` 三條分支(per-user / static / none)的選擇邏輯。
  - StaticTokenVerifier 對 token 的判斷(向後相容)。
  - PerUserTokenVerifier.verify_token 對後端 authorize 回應的處理
    (ok / invalid_key / quota_exceeded / HTTP error / 快取 fallback)。

HTTP transport 的實際攔截(401 / 200)由 FastMCP 框架負責,框架自己有測試,
我們不在這裡覆寫;這裡只驗 verifier 回 AccessToken vs None。
"""

from unittest.mock import patch

import httpx
import pytest
import respx
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from trading_agent_mcp import tools as _tools  # noqa: F401 —— 觸發 @mcp.tool 註冊
from trading_agent_mcp.auth import AUTHORIZE_PATH, PerUserTokenVerifier, _scopes_for_tier
from trading_agent_mcp.server import _build_auth, mcp

# authorize endpoint 的完整 URL(base_url + path),respx 用來比對。
_API_BASE = "http://test-api"
_AUTHORIZE_URL = f"{_API_BASE}{AUTHORIZE_PATH}"


# ============================================================
# _build_auth():分支選擇邏輯
# ============================================================


def test_no_token_returns_none() -> None:
    """per-user 關 + mcp_bearer_token 為空 → 不啟 auth(本機 stdio 開發用)。"""
    with patch("trading_agent_mcp.server.settings") as s:
        s.mcp_per_user_auth = False
        s.mcp_bearer_token = ""
        assert _build_auth() is None


def test_static_token_set_returns_static_verifier() -> None:
    """per-user 關 + 非空 token → StaticTokenVerifier(向後相容)。"""
    with patch("trading_agent_mcp.server.settings") as s:
        s.mcp_per_user_auth = False
        s.mcp_bearer_token = "test-secret"
        auth = _build_auth()
        assert isinstance(auth, StaticTokenVerifier)


def test_per_user_enabled_returns_per_user_verifier() -> None:
    """per-user 開 + base URL 有值 → PerUserTokenVerifier。"""
    with patch("trading_agent_mcp.server.settings") as s:
        s.mcp_per_user_auth = True
        s.mcp_api_base_url = _API_BASE
        s.mcp_api_auth_token = "service-token"
        s.mcp_authorize_cache_ttl = 20.0
        auth = _build_auth()
        assert isinstance(auth, PerUserTokenVerifier)


def test_per_user_without_base_url_falls_back_to_static() -> None:
    """per-user 開但 base URL 為空 → 退回 StaticTokenVerifier(避免無法驗證)。"""
    with patch("trading_agent_mcp.server.settings") as s:
        s.mcp_per_user_auth = True
        s.mcp_api_base_url = ""
        s.mcp_bearer_token = "test-secret"
        auth = _build_auth()
        assert isinstance(auth, StaticTokenVerifier)


# ============================================================
# StaticTokenVerifier 行為(向後相容)
# ============================================================


async def test_static_verifier_accepts_correct_token() -> None:
    with patch("trading_agent_mcp.server.settings") as s:
        s.mcp_per_user_auth = False
        s.mcp_bearer_token = "test-secret"
        auth = _build_auth()
        assert auth is not None
        access = await auth.verify_token("test-secret")
        assert access is not None
        assert access.client_id == "investor-db-default"


async def test_static_verifier_rejects_wrong_token() -> None:
    with patch("trading_agent_mcp.server.settings") as s:
        s.mcp_per_user_auth = False
        s.mcp_bearer_token = "test-secret"
        auth = _build_auth()
        assert auth is not None
        assert await auth.verify_token("wrong") is None
        assert await auth.verify_token("") is None


# ============================================================
# tier → scopes 對應
# ============================================================


def test_scopes_for_tier() -> None:
    assert _scopes_for_tier("free") == ["tier:free"]
    assert _scopes_for_tier("pro") == ["tier:pro"]


# ============================================================
# PerUserTokenVerifier.verify_token
# ============================================================


@pytest.fixture
async def verifier() -> PerUserTokenVerifier:
    """每個測試一個獨立 verifier(獨立快取),用完關掉 client。"""
    v = PerUserTokenVerifier(
        api_base_url=_API_BASE,
        service_token="service-token",
        cache_ttl=20.0,
    )
    yield v
    await v.aclose()


async def test_verify_ok_returns_access_token(verifier: PerUserTokenVerifier) -> None:
    """ok=true → AccessToken,client_id=user_id,scopes 反映 tier。"""
    with respx.mock as mock:
        route = mock.post(_AUTHORIZE_URL).respond(
            200,
            json={
                "ok": True,
                "user_id": "user-42",
                "tier": "pro",
                "used_today": 3,
                "daily_limit": 1000,
            },
        )
        access = await verifier.verify_token("user-key-abc")

    assert isinstance(access, AccessToken)
    assert access.client_id == "user-42"
    assert access.scopes == ["tier:pro"]
    assert access.token == "user-key-abc"
    assert access.claims["tier"] == "pro"
    assert access.claims["daily_limit"] == 1000
    # 驗證真的有 POST,且 body 帶 key/action/surface、header 帶 service token。
    assert route.called
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer service-token"
    import json as _json

    body = _json.loads(sent.content)
    assert body == {"key": "user-key-abc", "action": "connect", "surface": "mcp"}


async def test_verify_free_tier_scopes(verifier: PerUserTokenVerifier) -> None:
    """free tier → scopes 只有 tier:free(拿不到 tier:pro)。"""
    with respx.mock as mock:
        mock.post(_AUTHORIZE_URL).respond(
            200, json={"ok": True, "user_id": "u1", "tier": "free"}
        )
        access = await verifier.verify_token("free-key")

    assert access is not None
    assert access.scopes == ["tier:free"]
    assert "tier:pro" not in access.scopes


async def test_verify_invalid_key_returns_none(verifier: PerUserTokenVerifier) -> None:
    """ok=false reason=invalid_key → None。"""
    with respx.mock as mock:
        mock.post(_AUTHORIZE_URL).respond(
            200, json={"ok": False, "reason": "invalid_key"}
        )
        assert await verifier.verify_token("bad-key") is None


async def test_verify_revoked_returns_none(verifier: PerUserTokenVerifier) -> None:
    """ok=false reason=revoked → None。"""
    with respx.mock as mock:
        mock.post(_AUTHORIZE_URL).respond(200, json={"ok": False, "reason": "revoked"})
        assert await verifier.verify_token("revoked-key") is None


async def test_verify_quota_exceeded_returns_none(
    verifier: PerUserTokenVerifier,
) -> None:
    """ok=false reason=quota_exceeded → None(配額由後端 enforce)。"""
    with respx.mock as mock:
        mock.post(_AUTHORIZE_URL).respond(
            200, json={"ok": False, "reason": "quota_exceeded"}
        )
        assert await verifier.verify_token("over-quota-key") is None


async def test_empty_token_returns_none_without_call(
    verifier: PerUserTokenVerifier,
) -> None:
    """空 token 直接 None,不打後端。"""
    with respx.mock as mock:
        route = mock.post(_AUTHORIZE_URL).respond(200, json={"ok": True})
        assert await verifier.verify_token("") is None
        assert not route.called


async def test_http_error_without_cache_returns_none(
    verifier: PerUserTokenVerifier,
) -> None:
    """authorize 回 5xx 且無快取 → None(寧可 401 也不放行未驗證的 key)。"""
    with respx.mock as mock:
        mock.post(_AUTHORIZE_URL).respond(503)
        assert await verifier.verify_token("some-key") is None


async def test_network_error_without_cache_returns_none(
    verifier: PerUserTokenVerifier,
) -> None:
    """authorize 連線失敗且無快取 → None。"""
    with respx.mock as mock:
        mock.post(_AUTHORIZE_URL).mock(side_effect=httpx.ConnectError("boom"))
        assert await verifier.verify_token("some-key") is None


async def test_http_error_falls_back_to_cache(
    verifier: PerUserTokenVerifier,
) -> None:
    """先成功(填快取)→ 後 5xx → TTL 內沿用快取的 tier,回 AccessToken(stale)。"""
    with respx.mock as mock:
        mock.post(_AUTHORIZE_URL).respond(
            200, json={"ok": True, "user_id": "u9", "tier": "pro"}
        )
        first = await verifier.verify_token("cached-key")
    assert first is not None

    # 第二次 authorize 暫時失敗 —— 應沿用快取。
    with respx.mock as mock:
        mock.post(_AUTHORIZE_URL).respond(500)
        second = await verifier.verify_token("cached-key")

    assert second is not None
    assert second.client_id == "u9"
    assert second.scopes == ["tier:pro"]
    assert second.claims.get("stale") is True


async def test_explicit_failure_clears_cache(
    verifier: PerUserTokenVerifier,
) -> None:
    """先成功填快取 → 後端明確回 revoked → 清快取,之後即使暫時失敗也不放行。"""
    with respx.mock as mock:
        mock.post(_AUTHORIZE_URL).respond(
            200, json={"ok": True, "user_id": "u3", "tier": "pro"}
        )
        assert await verifier.verify_token("k") is not None

    # ok=false → None 且清快取。
    with respx.mock as mock:
        mock.post(_AUTHORIZE_URL).respond(200, json={"ok": False, "reason": "revoked"})
        assert await verifier.verify_token("k") is None

    # 之後 5xx —— 因快取已清,不再 fallback。
    with respx.mock as mock:
        mock.post(_AUTHORIZE_URL).respond(500)
        assert await verifier.verify_token("k") is None


async def test_expired_cache_does_not_fall_back() -> None:
    """快取過 TTL 後不再 fallback(cache_ttl=0 模擬立即過期)。"""
    v = PerUserTokenVerifier(
        api_base_url=_API_BASE, service_token="svc", cache_ttl=0.0
    )
    try:
        with respx.mock as mock:
            mock.post(_AUTHORIZE_URL).respond(
                200, json={"ok": True, "user_id": "u", "tier": "pro"}
            )
            assert await v.verify_token("k") is not None
        # TTL=0 → 立即過期,5xx 時不 fallback。
        with respx.mock as mock:
            mock.post(_AUTHORIZE_URL).respond(500)
            assert await v.verify_token("k") is None
    finally:
        await v.aclose()


# ============================================================
# Tier gating:execute_readonly_sql 的 require_scopes("tier:pro")
# ============================================================


def _access_token(scopes: list[str]) -> AccessToken:
    return AccessToken(token="t", client_id="u", scopes=scopes, claims={})


async def test_sql_tool_gated_to_pro_tier() -> None:
    """execute_readonly_sql 帶 auth check:pro 放行,free / 無 token 擋下。"""
    from fastmcp.server.auth import AuthContext, run_auth_checks

    # 從 provider 層拿到帶 auth 的 tool 物件。
    tools = await mcp._local_provider.list_tools()
    sql_tool = next(t for t in tools if t.name == "execute_readonly_sql")
    assert sql_tool.auth is not None, "execute_readonly_sql 應有 auth check"

    # pro tier → 通過。
    ok = await run_auth_checks(
        sql_tool.auth, AuthContext(token=_access_token(["tier:pro"]), component=sql_tool)
    )
    assert ok is True

    # free tier → 擋下。
    denied = await run_auth_checks(
        sql_tool.auth, AuthContext(token=_access_token(["tier:free"]), component=sql_tool)
    )
    assert denied is False

    # 無 token → 擋下。
    none_token = await run_auth_checks(
        sql_tool.auth, AuthContext(token=None, component=sql_tool)
    )
    assert none_token is False


async def test_structured_tool_not_gated() -> None:
    """structured tool(如 list_companies)沒有 auth check,free 也能用。"""
    tools = await mcp._local_provider.list_tools()
    plain_tool = next(t for t in tools if t.name == "list_companies")
    assert plain_tool.auth is None
