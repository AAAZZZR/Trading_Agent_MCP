"""server 端 auth 設定的單元測試。

涵蓋:
  - `_build_auth()` 三條分支(per-user / static / none)的選擇邏輯。
  - StaticTokenVerifier 對 token 的判斷(向後相容)。
  - PerUserTokenVerifier.verify_token 直讀 SaaS DB 的各種路徑
    (有效 / 撤銷 / 查無此 key / DB 掛掉 / 正向快取 / stale 沿用 / 額度分流)。
  - AuthErrorMessageMiddleware 對 401 回應的文案改寫。

一律 mock `saas_db` 的三個查詢函式(不連真 DB)—— 它們是模組層函式,
auth.py 透過 `saas_db.xxx()` 呼叫,monkeypatch 就能攔;比假造 asyncpg pool 乾淨,
也不會綁死 SQL 的實作細節(SQL 本身由 tests/test_saas_db.py 顧)。

HTTP transport 的實際攔截(401 / 200)由 FastMCP 框架負責,框架自己有測試,
我們不在這裡覆寫;這裡只驗 verifier 回 AccessToken vs None。
"""

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from trading_agent_mcp import tools as _tools  # noqa: F401 —— 觸發 @mcp.tool 註冊
from trading_agent_mcp.auth import (
    AUTH_ERROR_DESCRIPTION,
    CACHE_MAX_SIZE,
    QUOTA_CACHE_MAX_SIZE,
    TIER_DAILY_LIMITS,
    AuthErrorMessageMiddleware,
    MisconfiguredVerifier,
    PerUserTokenVerifier,
    _CachedQuota,
    _scopes_for_tier,
)
from trading_agent_mcp.server import _build_auth, mcp
from trading_agent_mcp.settings import Settings

_SAAS_DSN = "postgresql://saas:pw@test-db:5432/zeabur"

# FastMCP 框架寫死、我們要換掉的那句話。
_FRAMEWORK_ADVICE = "clear authentication tokens"


def _key_hash(token: str) -> str:
    """跟 auth.py / API repo 同一套 hash —— 快取以它為 key,測試要對得上。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _row(
    *,
    user_id: str = "user-42",
    api_key_id: str = "key-1",
    tier: str = "free",
    revoked_at: object = None,
) -> dict:
    """模擬 `saas_db.lookup_key` 回的一列(asyncpg Record 也是用 [] 取值)。"""
    return {
        "api_key_id": api_key_id,
        "user_id": user_id,
        "revoked_at": revoked_at,
        "tier": tier,
    }


def _patch_saas(monkeypatch, *, lookup: AsyncMock, count: AsyncMock | None = None):
    """把 saas_db 的查詢換成 mock,回傳 (lookup, count) 方便斷言呼叫次數。"""
    count = count or AsyncMock(return_value=0)
    monkeypatch.setattr("trading_agent_mcp.saas_db.lookup_key", lookup)
    monkeypatch.setattr("trading_agent_mcp.saas_db.count_usage_24h", count)
    return lookup, count


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
    """per-user 開 + SaaS DSN 有值 → PerUserTokenVerifier。"""
    with patch("trading_agent_mcp.server.settings") as s:
        s.mcp_per_user_auth = True
        s.mcp_saas_database_url = _SAAS_DSN
        s.mcp_auth_cache_ttl = 300.0
        s.mcp_auth_stale_ttl = 3600.0
        s.mcp_quota_cache_ttl = 60.0
        auth = _build_auth()
        assert isinstance(auth, PerUserTokenVerifier)


def test_per_user_without_saas_dsn_falls_back_to_static() -> None:
    """per-user 開但 SaaS DSN 為空 → 退回 StaticTokenVerifier(沒 DSN 驗不了 key)。"""
    with patch("trading_agent_mcp.server.settings") as s:
        s.mcp_per_user_auth = True
        s.mcp_saas_database_url = ""
        s.mcp_bearer_token = "test-secret"
        auth = _build_auth()
        assert isinstance(auth, StaticTokenVerifier)


async def test_per_user_without_dsn_or_token_rejects_instead_of_opening_up() -> None:
    """per-user 開著卻兩個憑據來源都沒有 → 一律 401,**不可以**退回「不啟用 auth」。

    這條是安全防線:mcp_per_user_auth 預設為 True,少設一個 MCP_SAAS_DATABASE_URL
    就把整台對外 server 敞開,是最糟的失敗模式。
    """
    with patch("trading_agent_mcp.server.settings") as s:
        s.mcp_per_user_auth = True
        s.mcp_saas_database_url = ""
        s.mcp_bearer_token = ""
        auth = _build_auth()

    assert isinstance(auth, MisconfiguredVerifier)
    assert auth is not None
    assert await auth.verify_token("idb_anything") is None


def test_build_auth_passes_settings_ttls() -> None:
    """三個 TTL 由 settings 帶進 verifier(不是寫死)。"""
    with patch("trading_agent_mcp.server.settings") as s:
        s.mcp_per_user_auth = True
        s.mcp_saas_database_url = _SAAS_DSN
        s.mcp_auth_cache_ttl = 111.0
        s.mcp_auth_stale_ttl = 222.0
        s.mcp_quota_cache_ttl = 333.0
        auth = _build_auth()
    assert isinstance(auth, PerUserTokenVerifier)
    assert auth._cache_ttl == 111.0
    assert auth._stale_ttl == 222.0
    assert auth._quota_ttl == 333.0


def test_default_ttls() -> None:
    """預設值:正向快取 5 分鐘、stale 60 分鐘、額度快取 60 秒。"""
    s = Settings()
    assert s.mcp_auth_cache_ttl == 300.0
    assert s.mcp_auth_stale_ttl == 3600.0
    assert s.mcp_quota_cache_ttl == 60.0


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
# tier → scopes / 額度上限對應
# ============================================================


def test_scopes_for_tier() -> None:
    assert _scopes_for_tier("free") == ["tier:free"]
    assert _scopes_for_tier("pro") == ["tier:pro"]


def test_tier_daily_limits_mirror_api_billing_config() -> None:
    """free = 200 / rolling 24h、pro = 不限 —— 與 API repo billing_config.py 一致。"""
    assert TIER_DAILY_LIMITS["free"] == 200
    assert TIER_DAILY_LIMITS["pro"] is None


# ============================================================
# PerUserTokenVerifier.verify_token —— 正常路徑
# ============================================================


@pytest.fixture
def verifier() -> PerUserTokenVerifier:
    """每個測試一個獨立 verifier(獨立快取)。"""
    return PerUserTokenVerifier(cache_ttl=300.0, stale_ttl=3600.0, quota_ttl=60.0)


async def test_valid_free_key_returns_access_token(monkeypatch, verifier) -> None:
    """有效 key + free tier → AccessToken,scopes=tier:free、claims 帶身分與額度。"""
    lookup, count = _patch_saas(
        monkeypatch,
        lookup=AsyncMock(return_value=_row(tier="free")),
        count=AsyncMock(return_value=7),
    )

    access = await verifier.verify_token("user-key-abc")

    assert isinstance(access, AccessToken)
    assert access.token == "user-key-abc"
    assert access.client_id == "user-42"
    assert access.scopes == ["tier:free"]
    assert "tier:pro" not in access.scopes
    assert access.claims["tier"] == "free"
    assert access.claims["user_id"] == "user-42"
    assert access.claims["api_key_id"] == "key-1"
    assert access.claims["quota_exceeded"] is False
    assert access.claims["used_today"] == 7
    assert access.claims["daily_limit"] == 200
    # 查的是 hash,不是明文 key。
    lookup.assert_awaited_once_with(_key_hash("user-key-abc"))
    count.assert_awaited_once_with("user-42")


async def test_pro_tier_skips_quota_query(monkeypatch, verifier) -> None:
    """pro tier → 拿到 tier:pro,而且完全不查用量(不限額,count(*) 純浪費)。"""
    _, count = _patch_saas(monkeypatch, lookup=AsyncMock(return_value=_row(tier="pro")))

    access = await verifier.verify_token("pro-key")

    assert access is not None
    assert access.scopes == ["tier:pro"]
    assert access.claims["quota_exceeded"] is False
    assert access.claims["daily_limit"] is None
    count.assert_not_awaited()


async def test_missing_subscription_row_defaults_to_free(monkeypatch, verifier) -> None:
    """沒有 subscriptions 列 → SQL 的 COALESCE 會給 'free',verifier 照 free 處理。

    (COALESCE 的參數綁定本身由 tests/test_saas_db.py 驗。)
    """
    _patch_saas(monkeypatch, lookup=AsyncMock(return_value=_row(tier="free")))

    access = await verifier.verify_token("no-sub-key")

    assert access is not None
    assert access.scopes == ["tier:free"]
    assert access.claims["daily_limit"] == 200


async def test_unknown_tier_falls_back_to_free_limit(monkeypatch, verifier) -> None:
    """未知 tier(例如日後新增方案還沒同步過來)→ 用 free 的上限,不會變成無限。"""
    _patch_saas(
        monkeypatch,
        lookup=AsyncMock(return_value=_row(tier="enterprise")),
        count=AsyncMock(return_value=5),
    )

    access = await verifier.verify_token("weird-tier-key")

    assert access is not None
    assert access.scopes == ["tier:enterprise"]
    assert access.claims["daily_limit"] == 200


async def test_empty_token_returns_none_without_query(monkeypatch, verifier) -> None:
    """空 token 直接 None,不碰 DB。"""
    lookup, _ = _patch_saas(monkeypatch, lookup=AsyncMock(return_value=_row()))
    assert await verifier.verify_token("") is None
    lookup.assert_not_awaited()


async def test_unknown_key_returns_none(monkeypatch, verifier) -> None:
    """key 不存在(lookup 回 None)→ None。"""
    _patch_saas(monkeypatch, lookup=AsyncMock(return_value=None))
    assert await verifier.verify_token("ghost-key") is None


async def test_revoked_key_returns_none(monkeypatch, verifier) -> None:
    """revoked_at 非 None → None。"""
    _patch_saas(monkeypatch, lookup=AsyncMock(return_value=_row(revoked_at="2026-08-01")))
    assert await verifier.verify_token("revoked-key") is None


# ============================================================
# 快取語意
# ============================================================


async def test_cache_hit_skips_db(monkeypatch, verifier) -> None:
    """TTL 內第二次驗證完全不碰 DB —— 這是整個改動的重點。"""
    lookup, count = _patch_saas(
        monkeypatch,
        lookup=AsyncMock(return_value=_row(tier="free")),
        count=AsyncMock(return_value=1),
    )

    first = await verifier.verify_token("k")
    second = await verifier.verify_token("k")

    assert first is not None and second is not None
    assert second.client_id == "user-42"
    assert lookup.await_count == 1
    assert count.await_count == 1


async def test_expired_cache_requeries_db(monkeypatch) -> None:
    """正向快取過期 → 重查 DB(cache_ttl=0 模擬立即過期)。"""
    v = PerUserTokenVerifier(cache_ttl=0.0, stale_ttl=3600.0, quota_ttl=60.0)
    lookup, _ = _patch_saas(
        monkeypatch,
        lookup=AsyncMock(return_value=_row(tier="free")),
        count=AsyncMock(return_value=1),
    )

    assert await v.verify_token("k") is not None
    assert await v.verify_token("k") is not None
    assert lookup.await_count == 2


async def test_db_outage_serves_stale_cache(monkeypatch) -> None:
    """先成功一次 → DB 掛掉 → stale 窗口內仍放行(存了 key 就穩)。"""
    v = PerUserTokenVerifier(cache_ttl=0.0, stale_ttl=3600.0, quota_ttl=60.0)
    _patch_saas(
        monkeypatch,
        lookup=AsyncMock(return_value=_row(tier="pro")),
        count=AsyncMock(return_value=0),
    )
    assert await v.verify_token("k") is not None

    monkeypatch.setattr(
        "trading_agent_mcp.saas_db.lookup_key", AsyncMock(side_effect=OSError("boom"))
    )
    stale = await v.verify_token("k")

    assert stale is not None
    assert stale.client_id == "user-42"
    assert stale.scopes == ["tier:pro"]


async def test_stale_window_expiry_rejects(monkeypatch) -> None:
    """超過 stale_ttl 之後 DB 還是掛的 → 回 None(不放行驗不了的 key)。

    不真的等 60 分鐘 —— 直接把快取項的 stale_until 改成過去。
    """
    v = PerUserTokenVerifier(cache_ttl=0.0, stale_ttl=3600.0, quota_ttl=60.0)
    _patch_saas(monkeypatch, lookup=AsyncMock(return_value=_row(tier="pro")))
    assert await v.verify_token("k") is not None

    key_hash = _key_hash("k")
    v._cache[key_hash] = replace(v._cache[key_hash], stale_until=0.0)
    monkeypatch.setattr(
        "trading_agent_mcp.saas_db.lookup_key", AsyncMock(side_effect=OSError("boom"))
    )

    assert await v.verify_token("k") is None
    assert key_hash not in v._cache


async def test_db_outage_without_cache_rejects(monkeypatch, verifier) -> None:
    """沒快取 + DB 掛掉 → None(寧可 401 也不放行從未驗證過的 key)。"""
    _patch_saas(monkeypatch, lookup=AsyncMock(side_effect=OSError("boom")))
    assert await verifier.verify_token("never-seen") is None


async def test_revocation_clears_cache(monkeypatch) -> None:
    """撤銷是「明確失效」:立刻清快取,之後即使 DB 掛掉也不會被 stale 放行。"""
    v = PerUserTokenVerifier(cache_ttl=0.0, stale_ttl=3600.0, quota_ttl=60.0)
    _patch_saas(monkeypatch, lookup=AsyncMock(return_value=_row(tier="pro")))
    assert await v.verify_token("k") is not None

    monkeypatch.setattr(
        "trading_agent_mcp.saas_db.lookup_key",
        AsyncMock(return_value=_row(tier="pro", revoked_at="2026-08-01")),
    )
    assert await v.verify_token("k") is None
    assert _key_hash("k") not in v._cache

    # DB 之後掛掉也不能靠舊快取復活。
    monkeypatch.setattr(
        "trading_agent_mcp.saas_db.lookup_key", AsyncMock(side_effect=OSError("boom"))
    )
    assert await v.verify_token("k") is None


async def test_cache_key_is_hash_not_plaintext(monkeypatch, verifier) -> None:
    """快取以 sha256 為 key —— 長生命週期的 dict 裡不留明文憑證。"""
    _patch_saas(monkeypatch, lookup=AsyncMock(return_value=_row()))
    await verifier.verify_token("super-secret-key")
    assert "super-secret-key" not in verifier._cache
    assert _key_hash("super-secret-key") in verifier._cache


async def test_cache_evicts_oldest_beyond_max_size(monkeypatch, verifier) -> None:
    """快取有上限:塞滿 + 1 把 key 後,最舊的被淘汰、長度不超過 CACHE_MAX_SIZE。"""
    _patch_saas(monkeypatch, lookup=AsyncMock(return_value=_row(tier="pro")))

    for i in range(CACHE_MAX_SIZE + 1):
        assert await verifier.verify_token(f"key-{i}") is not None

    assert len(verifier._cache) == CACHE_MAX_SIZE
    assert _key_hash("key-0") not in verifier._cache  # 最舊 —— 被擠掉
    assert _key_hash(f"key-{CACHE_MAX_SIZE}") in verifier._cache  # 最新 —— 還在


async def test_cache_reinsert_refreshes_position(monkeypatch) -> None:
    """重新驗證會把 key 移到最新端 —— 活躍的 key 不會因為「先來的」先被淘汰。"""
    # cache_ttl=0 讓每次 verify 都真的重查 + 重寫快取(才會刷新位置)。
    v = PerUserTokenVerifier(cache_ttl=0.0, stale_ttl=3600.0, quota_ttl=60.0)
    _patch_saas(monkeypatch, lookup=AsyncMock(return_value=_row(tier="pro")))

    for i in range(CACHE_MAX_SIZE):
        assert await v.verify_token(f"key-{i}") is not None
    assert await v.verify_token("key-0") is not None  # 移到最新端
    assert await v.verify_token("fresh") is not None  # 擠掉的應是 key-1

    assert len(v._cache) == CACHE_MAX_SIZE
    assert _key_hash("key-0") in v._cache
    assert _key_hash("key-1") not in v._cache


# ============================================================
# 額度(quota)分流
# ============================================================


async def test_quota_exceeded_still_returns_access_token(monkeypatch, verifier) -> None:
    """超額 ≠ 壞 key:仍回 AccessToken(連線建得起來),只在 claims 標記。"""
    _patch_saas(
        monkeypatch,
        lookup=AsyncMock(return_value=_row(tier="free")),
        count=AsyncMock(return_value=200),
    )

    access = await verifier.verify_token("busy-key")

    assert access is not None
    assert access.claims["quota_exceeded"] is True
    assert access.claims["used_today"] == 200


async def test_quota_just_under_limit_not_exceeded(monkeypatch, verifier) -> None:
    """199 / 200 → 還沒超額。"""
    _patch_saas(
        monkeypatch,
        lookup=AsyncMock(return_value=_row(tier="free")),
        count=AsyncMock(return_value=199),
    )

    access = await verifier.verify_token("almost-key")

    assert access is not None
    assert access.claims["quota_exceeded"] is False


async def test_quota_query_failure_fails_open(monkeypatch, verifier) -> None:
    """額度查不到 → fail open(基礎設施抖動不該擋住使用者)。"""
    _patch_saas(
        monkeypatch,
        lookup=AsyncMock(return_value=_row(tier="free")),
        count=AsyncMock(side_effect=OSError("boom")),
    )

    access = await verifier.verify_token("k")

    assert access is not None
    assert access.claims["quota_exceeded"] is False
    assert access.claims["used_today"] is None
    # fail open 的結果不進快取,下次請求會再試一次。
    assert "user-42" not in verifier._quota_cache


async def test_quota_cache_avoids_recount(monkeypatch) -> None:
    """額度快取:認證快取過期而重查 DB 時,用量不跟著重數。"""
    v = PerUserTokenVerifier(cache_ttl=0.0, stale_ttl=3600.0, quota_ttl=60.0)
    lookup, count = _patch_saas(
        monkeypatch,
        lookup=AsyncMock(return_value=_row(tier="free")),
        count=AsyncMock(return_value=3),
    )

    assert await v.verify_token("k") is not None
    assert await v.verify_token("k") is not None

    assert lookup.await_count == 2
    assert count.await_count == 1


async def test_quota_cache_evicts_oldest_beyond_max_size(monkeypatch) -> None:
    """額度快取同樣有上限(每個 user 一項,不能無限長)。"""
    v = PerUserTokenVerifier(cache_ttl=0.0, stale_ttl=3600.0, quota_ttl=60.0)
    users = iter(range(QUOTA_CACHE_MAX_SIZE + 1))
    _patch_saas(
        monkeypatch,
        lookup=AsyncMock(side_effect=lambda _h: _row(user_id=f"u{next(users)}")),
        count=AsyncMock(return_value=1),
    )

    for i in range(QUOTA_CACHE_MAX_SIZE + 1):
        assert await v.verify_token(f"key-{i}") is not None

    assert len(v._quota_cache) == QUOTA_CACHE_MAX_SIZE
    assert "u0" not in v._quota_cache
    assert f"u{QUOTA_CACHE_MAX_SIZE}" in v._quota_cache


async def test_expired_quota_cache_recounts(monkeypatch) -> None:
    """額度快取過期 → 重新數一次(quota_ttl=0 模擬)。"""
    v = PerUserTokenVerifier(cache_ttl=0.0, stale_ttl=3600.0, quota_ttl=0.0)
    _, count = _patch_saas(
        monkeypatch,
        lookup=AsyncMock(return_value=_row(tier="free")),
        count=AsyncMock(return_value=1),
    )

    assert await v.verify_token("k") is not None
    assert await v.verify_token("k") is not None
    assert count.await_count == 2


def test_cached_quota_is_frozen() -> None:
    """快取項是 frozen dataclass —— 不會被誰不小心就地改掉。"""
    q = _CachedQuota(exceeded=False, used_today=1, daily_limit=200, expires_at=0.0)
    with pytest.raises(FrozenInstanceError):
        q.exceeded = True  # type: ignore[misc]


# ============================================================
# AuthErrorMessageMiddleware:401 文案改寫
# ============================================================


def _framework_401_app(status: int = 401):
    """模擬 FastMCP RequireAuthMiddleware 送出的錯誤回應(含那句誤導文案)。"""
    description = (
        "Authentication failed. Please clear authentication tokens in your MCP "
        "client and reconnect."
    )
    body = json.dumps({"error": "invalid_token", "error_description": description}).encode()

    async def app(scope, receive, send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", f'Bearer error_description="{description}"'.encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return app


async def _drive(app, scope: dict | None = None) -> list[dict]:
    """跑一次 middleware,收集它送出的 ASGI 訊息。"""
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    async def receive() -> dict:
        return {"type": "http.request"}

    await AuthErrorMessageMiddleware(app)(scope or {"type": "http"}, receive, send)
    return sent


def _headers(start_message: dict) -> dict[bytes, list[bytes]]:
    out: dict[bytes, list[bytes]] = {}
    for name, value in start_message["headers"]:
        out.setdefault(name.lower(), []).append(value)
    return out


async def test_middleware_rewrites_401_body_and_header() -> None:
    """401 → body 與 www-authenticate 都換成我們的文案,框架那句話不見。"""
    sent = await _drive(_framework_401_app())

    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    start, body = sent
    assert start["status"] == 401

    headers = _headers(start)
    www = headers[b"www-authenticate"][0].decode()
    assert AUTH_ERROR_DESCRIPTION in www
    assert _FRAMEWORK_ADVICE not in www

    payload = json.loads(body["body"])
    assert payload["error"] == "invalid_token"
    assert payload["error_description"] == AUTH_ERROR_DESCRIPTION
    assert _FRAMEWORK_ADVICE not in body["body"].decode()


async def test_middleware_keeps_content_length_consistent() -> None:
    """改寫過的 body 與 content-length 必須對得上,而且不能留下重複的 header。"""
    sent = await _drive(_framework_401_app())
    start, body = sent
    headers = _headers(start)

    assert len(headers[b"content-length"]) == 1
    assert len(headers[b"www-authenticate"]) == 1
    assert int(headers[b"content-length"][0]) == len(body["body"])


def test_middleware_description_is_ascii() -> None:
    """文案必須是純 ASCII —— HTTP header 值只能 latin-1,中文會炸。"""
    AUTH_ERROR_DESCRIPTION.encode("ascii")
    assert '"' not in AUTH_ERROR_DESCRIPTION  # 引號會破壞 header 的 quoted-string


async def test_middleware_passes_through_non_401() -> None:
    """非 401(這裡用 403 insufficient_scope 的形狀)原樣放行,文案不動。"""
    sent = await _drive(_framework_401_app(status=403))

    assert sent[0]["status"] == 403
    assert _FRAMEWORK_ADVICE in sent[1]["body"].decode()


async def test_middleware_passes_through_success() -> None:
    """200 回應完全不碰(含分段 body)。"""

    async def app(scope, receive, send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"he", "more_body": True})
        await send({"type": "http.response.body", "body": b"llo"})

    sent = await _drive(app)

    assert sent[0]["status"] == 200
    assert b"".join(m["body"] for m in sent[1:]) == b"hello"


async def test_middleware_ignores_non_http_scope() -> None:
    """非 http scope(lifespan / websocket)直接放行,不做任何包裝。"""
    seen: list[dict] = []

    async def app(scope, receive, send) -> None:
        seen.append(scope)

    await _drive(app, scope={"type": "lifespan"})
    assert seen == [{"type": "lifespan"}]


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
    none_token = await run_auth_checks(sql_tool.auth, AuthContext(token=None, component=sql_tool))
    assert none_token is False


async def test_structured_tool_not_gated() -> None:
    """structured tool(如 list_companies)沒有 auth check,free 也能用。"""
    tools = await mcp._local_provider.list_tools()
    plain_tool = next(t for t in tools if t.name == "list_companies")
    assert plain_tool.auth is None
