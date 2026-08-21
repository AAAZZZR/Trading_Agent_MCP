"""OAuth(Google 上游)認證路徑的單元測試。

全部 mock —— 不連 Google、不連 DB:

  - 框架那半(驗 FastMCP JWT、換回上游 Google token)由 fastmcp 自己測,這裡
    直接把 `GoogleProvider.verify_token` 換掉,只驗**我們加的那兩層**:
    token 分流(`idb_` vs OAuth)與身分映射(Google sub → 我們的 users.id)。
  - `saas_db.upsert_google_user` / `count_usage_24h` 是模組層函式,monkeypatch
    就攔得到;SQL 本身由 tests/test_saas_db.py 顧。

最重要的兩條不變量(錯了整個功能會靜默壞掉,所以每條都有專屬測試):
  1. `idb_` 開頭的 token 走既有 bearer 路徑,**完全不碰** OAuth。
  2. 回出去的 AccessToken 的 `client_id` 是**我們的 uuid**,不是 Google 的 sub ——
     usage_events 的外鍵認的是前者。
"""

from unittest.mock import AsyncMock

import pytest
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.google import GoogleProvider

from trading_agent_mcp.oauth import (
    ACCESS_TOKEN_EXPIRY_S,
    API_KEY_PREFIX,
    GOOGLE_SCOPES,
    REFRESH_TOKEN_EXPIRY_S,
    StockfactsGoogleProvider,
    build_oauth_provider,
)

_SAAS_DSN = "postgresql://saas:pw@test-db:5432/zeabur"
_USER_ID = "3d516d38-6682-4520-a776-f5ba324c6e21"
_GOOGLE_SUB = "google-sub-1"
_EXPIRES_AT = 1_800_000_000


def _build(**overrides) -> StockfactsGoogleProvider:
    """組一個 provider(不連線 —— PostgreSQLStore 是 lazy 的)。"""
    kwargs = {
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "public_base_url": "http://localhost:8791",
        "saas_database_url": _SAAS_DSN,
        "cache_ttl": 300.0,
        "stale_ttl": 3600.0,
        "quota_ttl": 60.0,
    }
    kwargs.update(overrides)
    provider = build_oauth_provider(**kwargs)
    assert provider is not None
    return provider


@pytest.fixture
def provider() -> StockfactsGoogleProvider:
    """每個測試一個獨立 provider(獨立的身分 / 額度快取)。"""
    return _build()


def _google_token(**claim_overrides) -> AccessToken:
    """模擬 `GoogleTokenVerifier` 驗完上游 token 之後回的那顆 AccessToken。"""
    claims = {
        "sub": _GOOGLE_SUB,
        "aud": "test-client-id",
        "email": "alice@example.com",
        # ⚠️ Google 的 tokeninfo 端點回的是**字串** "true"(不是 bool)。
        "email_verified": "true",
        "name": "Alice",
        "picture": "https://example.com/a.png",
    }
    claims.update(claim_overrides)
    return AccessToken(
        token="fastmcp-jwt",
        client_id=_GOOGLE_SUB,
        scopes=list(GOOGLE_SCOPES),
        expires_at=_EXPIRES_AT,
        claims=claims,
    )


def _patch_upstream(monkeypatch, result: AccessToken | None) -> AsyncMock:
    """把框架那半(super().verify_token)換掉。

    `GoogleProvider` 自己沒有定義 verify_token(它繼承 OAuthProvider 的),
    在它身上塞一個就正好卡在 `StockfactsGoogleProvider.super()` 的位置。
    """
    mock = AsyncMock(return_value=result)
    monkeypatch.setattr(GoogleProvider, "verify_token", mock, raising=False)
    return mock


def _patch_saas(
    monkeypatch,
    *,
    upsert: AsyncMock | None = None,
    count: AsyncMock | None = None,
) -> tuple[AsyncMock, AsyncMock]:
    upsert = upsert or AsyncMock(return_value={"user_id": _USER_ID, "tier": "free"})
    count = count or AsyncMock(return_value=0)
    monkeypatch.setattr("trading_agent_mcp.saas_db.upsert_google_user", upsert)
    monkeypatch.setattr("trading_agent_mcp.saas_db.count_usage_24h", count)
    return upsert, count


# ============================================================
# 兩條路的分流
# ============================================================


async def test_api_key_goes_to_bearer_verifier(monkeypatch, provider) -> None:
    """`idb_` 開頭 → 原封不動交給既有的 PerUserTokenVerifier,完全不碰 OAuth。"""
    upstream = _patch_upstream(monkeypatch, _google_token())
    expected = AccessToken(token="idb_x", client_id=_USER_ID, scopes=["tier:free"], claims={})
    bearer = AsyncMock(return_value=expected)
    monkeypatch.setattr(provider._api_key_verifier, "verify_token", bearer)

    access = await provider.verify_token("idb_abcdef")

    assert access is expected
    bearer.assert_awaited_once_with("idb_abcdef")
    upstream.assert_not_awaited()


def test_api_key_prefix_matches_the_api_repo() -> None:
    """前綴是跨 repo 的格式契約(API repo saas/auth.py:_KEY_PREFIX),不能亂改。"""
    assert API_KEY_PREFIX == "idb_"


async def test_empty_token_returns_none(monkeypatch, provider) -> None:
    """空 token 直接拒絕,兩條路都不用走。"""
    upstream = _patch_upstream(monkeypatch, _google_token())
    assert await provider.verify_token("") is None
    upstream.assert_not_awaited()


async def test_oauth_token_delegates_to_framework(monkeypatch, provider) -> None:
    """非 `idb_` → 交給框架驗;框架說不行就是不行(不查 DB)。"""
    upstream = _patch_upstream(monkeypatch, None)
    upsert, _ = _patch_saas(monkeypatch)

    assert await provider.verify_token("some.jwt.token") is None

    upstream.assert_awaited_once_with("some.jwt.token")
    upsert.assert_not_awaited()


# ============================================================
# 身分映射:Google sub → 我們的 users.id
# ============================================================


async def test_identity_is_mapped_to_our_user(monkeypatch, provider) -> None:
    """Google claims → upsert → AccessToken 換成我們的身分與額度。"""
    _patch_upstream(monkeypatch, _google_token())
    upsert, _ = _patch_saas(monkeypatch)

    access = await provider.verify_token("some.jwt.token")

    assert access is not None
    upsert.assert_awaited_once_with(
        google_sub=_GOOGLE_SUB,
        email="alice@example.com",
        name="Alice",
        avatar_url="https://example.com/a.png",
    )
    # ⚠️ 命脈:client_id 必須是我們的 uuid,usage_events 的外鍵才對得上。
    assert access.client_id == _USER_ID
    assert access.claims["user_id"] == _USER_ID
    assert access.scopes == ["tier:free"]
    assert access.claims["tier"] == "free"
    assert access.claims["auth_method"] == "oauth"
    # OAuth session 沒有 API key。
    assert access.claims["api_key_id"] is None
    # 框架算好的效期不能弄丟 —— client 靠它決定何時 refresh。
    assert access.expires_at == _EXPIRES_AT
    # Google 原本的 claims 要留著(下游要拿 email / 顯示名)。
    assert access.claims["sub"] == _GOOGLE_SUB
    assert access.claims["email"] == "alice@example.com"


async def test_pro_tier_gets_pro_scope(monkeypatch, provider) -> None:
    """tier 反映在 scopes 上 —— tools.py 的 require_scopes("tier:pro") 靠它。"""
    _patch_upstream(monkeypatch, _google_token())
    _patch_saas(monkeypatch, upsert=AsyncMock(return_value={"user_id": _USER_ID, "tier": "pro"}))

    access = await provider.verify_token("t")

    assert access is not None
    assert access.scopes == ["tier:pro"]


async def test_email_bound_to_another_google_account_is_rejected(monkeypatch, provider) -> None:
    """upsert 回 0 列 = email 已綁在別的 Google 帳號 → fail closed。"""
    _patch_upstream(monkeypatch, _google_token())
    _patch_saas(monkeypatch, upsert=AsyncMock(return_value=None))

    assert await provider.verify_token("t") is None


# ============================================================
# email 必須是「有、而且已驗證」
# ============================================================


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"email": None}, id="no-email"),
        pytest.param({"email": ""}, id="empty-email"),
        pytest.param({"email_verified": False}, id="verified-bool-false"),
        # ⚠️ 這條是重點:Google 回的是字串,而 "false" 是 Python 真值 ——
        #    用 `if value:` 判斷會把未驗證的 email 直接放行。
        pytest.param({"email_verified": "false"}, id="verified-string-false"),
        pytest.param({"email_verified": None}, id="verified-missing"),
    ],
)
async def test_unverified_email_is_rejected(monkeypatch, provider, overrides) -> None:
    """沒有可信 email 就對不到帳號 —— 拒絕,不猜。"""
    _patch_upstream(monkeypatch, _google_token(**overrides))
    upsert, _ = _patch_saas(monkeypatch)

    assert await provider.verify_token("t") is None
    upsert.assert_not_awaited()


async def test_verified_email_bool_true_is_accepted(monkeypatch, provider) -> None:
    """v2 userinfo 端點回的是真正的 bool —— 兩種型別都要接。"""
    _patch_upstream(monkeypatch, _google_token(email_verified=True))
    _patch_saas(monkeypatch)

    assert await provider.verify_token("t") is not None


async def test_missing_sub_is_rejected(monkeypatch, provider) -> None:
    """沒有 sub(理論上不會發生)—— 防呆,不放行。"""
    _patch_upstream(monkeypatch, _google_token(sub=None))
    upsert, _ = _patch_saas(monkeypatch)

    assert await provider.verify_token("t") is None
    upsert.assert_not_awaited()


# ============================================================
# 身分快取(語意抄 bearer 路徑:命中不碰 DB、DB 掛掉沿用 stale)
# ============================================================


async def test_identity_cache_avoids_repeat_upsert(monkeypatch, provider) -> None:
    """第二次驗證不再 upsert —— upsert 是寫入交易,每個請求做一次太貴。"""
    _patch_upstream(monkeypatch, _google_token())
    upsert, _ = _patch_saas(monkeypatch)

    assert await provider.verify_token("t") is not None
    assert await provider.verify_token("t") is not None

    assert upsert.await_count == 1


async def test_db_outage_serves_stale_identity(monkeypatch) -> None:
    """DB 暫時不可用 → 沿用最近解析過的身分(agent 的長工作階段不該整批被踢)。"""
    p = _build(cache_ttl=0.0, stale_ttl=3600.0)
    _patch_upstream(monkeypatch, _google_token())
    upsert, _ = _patch_saas(monkeypatch)

    assert await p.verify_token("t") is not None

    upsert.side_effect = OSError("db down")
    access = await p.verify_token("t")

    assert access is not None
    assert access.client_id == _USER_ID


async def test_stale_window_expiry_rejects(monkeypatch) -> None:
    """連 stale 窗口都過了 → 拒絕(不放行無法驗證的身分)。"""
    p = _build(cache_ttl=0.0, stale_ttl=0.0)
    _patch_upstream(monkeypatch, _google_token())
    upsert, _ = _patch_saas(monkeypatch)

    assert await p.verify_token("t") is not None

    upsert.side_effect = OSError("db down")
    assert await p.verify_token("t") is None


async def test_db_outage_without_cache_rejects(monkeypatch, provider) -> None:
    """從沒解析過 + DB 掛掉 → 拒絕。"""
    _patch_upstream(monkeypatch, _google_token())
    _patch_saas(monkeypatch, upsert=AsyncMock(side_effect=OSError("db down")))

    assert await provider.verify_token("t") is None


# ============================================================
# 額度(與 bearer 共用同一份 QuotaCache)
# ============================================================


async def test_quota_exceeded_still_returns_access_token(monkeypatch, provider) -> None:
    """超額 ≠ 認證失敗:連線要建得起來,只有 tool 呼叫被 usage.py 擋下。"""
    _patch_upstream(monkeypatch, _google_token())
    _patch_saas(monkeypatch, count=AsyncMock(return_value=200))

    access = await provider.verify_token("t")

    assert access is not None
    assert access.claims["quota_exceeded"] is True
    assert access.claims["used_today"] == 200
    assert access.claims["daily_limit"] == 200


async def test_pro_tier_skips_quota_query(monkeypatch, provider) -> None:
    """pro 不限額 —— 連 count(*) 都不用跑。"""
    _patch_upstream(monkeypatch, _google_token())
    _, count = _patch_saas(
        monkeypatch,
        upsert=AsyncMock(return_value={"user_id": _USER_ID, "tier": "pro"}),
    )

    access = await provider.verify_token("t")

    assert access is not None
    assert access.claims["quota_exceeded"] is False
    count.assert_not_awaited()


async def test_quota_cache_is_shared_with_bearer_path(provider) -> None:
    """兩條路共用**同一個** QuotaCache 實例 —— 同一個人換條路進來額度是同一份。"""
    assert provider._quota_cache is provider._api_key_verifier.quota_cache


# ============================================================
# build_oauth_provider:設定不全就回 None(不丟例外)
# ============================================================


@pytest.mark.parametrize(
    "missing",
    ["client_id", "client_secret", "public_base_url", "saas_database_url"],
)
def test_incomplete_config_returns_none(missing) -> None:
    """四個設定缺任何一個 → None,讓 server.py 退回 bearer 模式,**不 crash**。"""
    kwargs = {
        "client_id": "cid",
        "client_secret": "csecret",
        "public_base_url": "http://localhost:8791",
        "saas_database_url": _SAAS_DSN,
        "cache_ttl": 300.0,
        "stale_ttl": 3600.0,
        "quota_ttl": 60.0,
    }
    kwargs[missing] = ""
    assert build_oauth_provider(**kwargs) is None


def test_token_expiry_settings(provider) -> None:
    """access token 1 小時、refresh token 30 天(不是 fastmcp 預設的一年)。"""
    assert ACCESS_TOKEN_EXPIRY_S == 3600
    assert REFRESH_TOKEN_EXPIRY_S == 30 * 24 * 3600
    assert provider._fastmcp_access_token_expiry_seconds == ACCESS_TOKEN_EXPIRY_S
    assert provider._fallback_refresh_token_expiry_seconds == REFRESH_TOKEN_EXPIRY_S


def test_consent_screen_stays_on(provider) -> None:
    """第一次授權要顯示同意頁 —— 關掉等於任何註冊過的 client 都能靜默拿資料。"""
    assert provider._require_authorization_consent is True


def test_transport_level_scope_check_is_disabled(provider) -> None:
    """required_scopes 必須清空,否則每個請求都 403(我們回的是 tier:* scope)。

    但對外公告的 scopes 仍然是 Google 那三個 —— 清空的只是傳輸層的比對。
    """
    assert provider.required_scopes == []
    assert provider.client_registration_options is not None
    assert provider.client_registration_options.valid_scopes == GOOGLE_SCOPES


def test_oauth_routes_are_published(provider) -> None:
    """OAuth 探索與流程的路由都掛上了 —— 少任何一條 `claude mcp add` 就走不完。"""
    paths = {route.path for route in provider.get_routes("/mcp")}

    assert "/.well-known/oauth-authorization-server" in paths
    assert "/.well-known/oauth-protected-resource/mcp" in paths
    assert "/authorize" in paths
    assert "/token" in paths
    # DCR:2026-07-28 spec 標為 deprecated 但保留至少 12 個月,舊 client 只認得它。
    assert "/register" in paths
    # Google 的 redirect URI 就是 <base_url> + 這條。
    assert "/auth/callback" in paths


def test_cimd_stays_enabled(provider) -> None:
    """CIMD 是 Claude Code / Desktop 在使用者發起路徑上用的那條,不能關。"""
    assert provider._cimd_manager is not None
