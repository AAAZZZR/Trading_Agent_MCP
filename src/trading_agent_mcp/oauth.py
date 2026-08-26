"""OAuth 2.1 授權流程(Google 上游)—— 讓使用者不必再手動貼 API key。

# 這在解什麼問題

bearer API key 那條路(`auth.py`)已經很穩,但它有個沒辦法用「更穩」解決的痛點:
使用者得先去網站建一把 key、複製、貼進 MCP client 的設定檔。換一台電腦要再貼一次,
key 打錯了看到的是 401。標準 MCP OAuth 把這一整段拿掉:

    claude mcp add → client 讀 /.well-known/... 發現授權伺服器
                   → 自己註冊(DCR /register)→ 彈瀏覽器 → 使用者用 Google 登入
                   → client 自動拿到 access + refresh token → 過期自動續

使用者從頭到尾沒看過任何一串憑證。這是主線;bearer key 路徑**保留並存**,
給 REST / 腳本型 agent 與用 email-密碼註冊的使用者。

# 兩條路怎麼並存

`StockfactsGoogleProvider.verify_token` 依 token 長相分流:`idb_` 開頭的一律
交給既有的 `PerUserTokenVerifier`(行為完全不變),其餘交給框架的 OAuth 驗證。
前綴是明確的格式契約(API repo `saas/auth.py:_KEY_PREFIX`),不會跟 OAuth 的
JWT 混淆。兩條路最後都產出**同一種** AccessToken:`client_id` 是我們自己的
users.id、claims 帶 tier / 額度 —— 所以 `usage.py` 的計量、tools.py 的 tier
gating 都不用知道使用者是從哪條路進來的。

# 為什麼一定要指定持久化的 client_storage

`OAuthProxy` 把**全部** OAuth 狀態放在 `client_storage`:client 註冊、上游 Google
token、授權交易、authorization code、JTI 對應、refresh token metadata。它的預設是
「資料目錄下的加密檔案」—— 在 Zeabur / k3s 上 pod 一換就整包蒸發,使用者剛授權完、
下一次 redeploy 就得重來,正是我們要消滅的病。所以固定用 `PostgreSQLStore`
指向 SaaS 控制面那顆 DB。

選 Postgres 而不是 Redis:`RedisStore` 需要 `py-key-value-aio[redis]` extra
(實測不裝會 ImportError),等於多一個相依、多一個環境變數、多一個要顧的服務;
Postgres 這條路用的是本 repo 早就有的 asyncpg + 早就有的 DSN,而且
`auto_create=True` 會自己建表,連 migration 都不用寫。

# 為什麼一定要加密

storage 裡有使用者的**上游 Google access / refresh token**。DB 被翻到就等於
別人拿到那些人的 Google 授權,所以外面一定要包 `FernetEncryptionWrapper`。
金鑰用 Google client secret 決定性推導(`source_material` + 固定 `salt`),
跨 pod 穩定、不必再多一個環境變數;`salt` 只是做領域隔離。
`raise_on_decryption_error=False` 也是刻意的:client secret 一旦輪替,舊資料就
解不開了,那時候正確的行為是「當作沒有這筆、請使用者重新授權」,而不是讓每個
請求都炸掉把整台 server 拖垮。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.google import GoogleProvider
from key_value.aio.stores.postgresql import PostgreSQLStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from trading_agent_mcp import consent_page, saas_db
from trading_agent_mcp.auth import (
    _TRANSIENT_DB_ERRORS,
    CACHE_MAX_SIZE,
    PerUserTokenVerifier,
    QuotaCache,
    _scopes_for_tier,
    remember_entry,
)

logger = logging.getLogger(__name__)

# 自家 API key 的前綴(API repo `saas/auth.py:_KEY_PREFIX`)。verify_token 靠它
# 在 O(1) 內把兩條認證路徑分開,不必先嘗試解 JWT 再回頭猜。
API_KEY_PREFIX = "idb_"

# 跟 Google 要的 scope。`openid` 是 OIDC 最小集;email 是**必要**的 —— 我們靠它
# 把 Google 帳號對到既有的 users 列(同一個人可能先用 email/密碼註冊過);
# profile 只是為了填 name / avatar,少了不影響登入但使用者列表會空一片。
GOOGLE_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

# FastMCP 自己簽發給 client 的 access token 效期:1 小時。
# 上游 Google access token 也差不多是這個數,但這裡刻意寫死而不是跟著上游 ——
# 有些 client(mcp-remote 之類)對「突然變短的效期」處理得不好,
# 固定 1 小時讓刷新節奏可預期。過期由 refresh token 自動換新,使用者無感。
ACCESS_TOKEN_EXPIRY_S = 3600

# refresh token 效期:30 天。這是「多久沒用就得重新用 Google 登入一次」的上限。
# Google 對 refresh token 不回 `refresh_expires_in`,不指定的話 fastmcp 預設給一年 ——
# 太長了:那等於一把在 client 設定檔裡躺一年的長效憑證。30 天是活躍使用者
# 完全不會碰到、閒置帳號又不至於長期掛著的折衷。
REFRESH_TOKEN_EXPIRY_S = 30 * 24 * 3600

# OAuth 狀態表的表名。跑在 warehouse 那顆 DB 上,而它已經有 ~25 張業務表,
# 預設的 `kv_store` 看不出是誰的;明確命名讓之後翻 DB 的人一眼知道歸屬。
OAUTH_STATE_TABLE = "mcp_oauth_state"

# 儲存加密金鑰的推導 salt(領域隔離用,不是祕密)。改它 = 既有 OAuth 狀態全部作廢。
STORAGE_SALT = "stockfacts-oauth-storage"


@dataclass(frozen=True)
class _CachedIdentity:
    """一個 Google 身分解析成我們自己的帳號之後的結果。

    時間用 `time.monotonic()`,語意與 `auth.py` 的 `_CachedAuth` 一致:
    fresh_until 之前直接用;之後正常會重查 DB,只有 DB 出事才拿 stale 頂著;
    stale_until 之後視同沒有快取。
    """

    user_id: str
    tier: str
    fresh_until: float
    stale_until: float


def _is_email_verified(value: object) -> bool:
    """Google 的 `email_verified` 到底是不是 true。

    ⚠️ 不能直接用真假值判斷:Google 的 tokeninfo 端點回的是**字串** `"true"` /
    `"false"`,而 `"false"` 是真值 —— 直接 `if value:` 會把未驗證的 email 放行。
    v2 userinfo 端點回的又是真正的 bool,所以兩種都要接。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


class StockfactsGoogleProvider(GoogleProvider):
    """Google OAuth proxy + 我們自己的身分/額度層。

    在 fastmcp `GoogleProvider` 上加兩件事:

    1. **混合驗證** —— `idb_` 開頭的 token 走既有的 bearer 路徑,其餘走 OAuth。
    2. **身分映射** —— 把 Google 的 `sub` 換成我們的 `users.id`,補上 tier 與額度,
       讓下游(計量、tier gating)看到的 AccessToken 兩條路長得一模一樣。

    metadata / authorize / token / register / callback 這些路由由父類提供,
    掛上這個 provider 就等於對外公告了 OAuth,`claude mcp add` 才發現得到。
    """

    def __init__(
        self,
        *,
        api_key_verifier: PerUserTokenVerifier,
        identity_cache_ttl: float,
        identity_stale_ttl: float,
        **google_kwargs: Any,
    ) -> None:
        """
        Args:
            api_key_verifier: 既有的 bearer API key verifier。這個 provider 不重寫
                那條路,只是在 token 長得像 API key 時原封不動轉交過去。
            identity_cache_ttl / identity_stale_ttl: Google sub → 我們的 user 這層
                對應的快取 / stale 窗口秒數(語意同 bearer 路徑的兩個 TTL)。
            **google_kwargs: 原樣傳給 `GoogleProvider`。
        """
        super().__init__(**google_kwargs)
        self._api_key_verifier = api_key_verifier
        # 共用**同一個** QuotaCache 實例:額度是 per-user 的,同一個人用 API key
        # 或用 OAuth session 打進來,計的是同一份 200 次(見 QuotaCache docstring)。
        self._quota_cache: QuotaCache = api_key_verifier.quota_cache
        self._identity_cache_ttl = identity_cache_ttl
        self._identity_stale_ttl = identity_stale_ttl
        # key = Google sub(不是 email:email 會變,sub 不會)。
        self._identity_cache: dict[str, _CachedIdentity] = {}

        # ⚠️ 關掉「傳輸層 required scopes 檢查」。
        # `OAuthProxy` 會把 required_scopes 設成 Google 那三個 scope,而 FastMCP 的
        # RequireAuthMiddleware 會拿它逐一比對 AccessToken.scopes —— 我們回的是
        # `tier:free` / `tier:pro`(bearer 路徑更是完全沒有 Google scope),
        # 不清掉的話**每一個請求都會 403 insufficient_scope**,兩條路都死。
        # 清掉並不會放寬任何東西:
        #   - 「這個 Google token 到底有沒有拿到 email / profile」仍由
        #     `GoogleTokenVerifier.required_scopes` 在驗上游 token 時把關(沒動);
        #   - 「這個 user 能不能用重量級 tool」由 tools.py 的 require_scopes("tier:pro") 把關;
        #   - 對外公告的 scopes_supported 走的是 client_registration_options.valid_scopes,
        #     不受這裡影響(metadata 照樣列出三個 Google scope)。
        self.required_scopes = []

    async def verify_token(self, token: str) -> AccessToken | None:
        """驗一個 bearer token —— 自家 API key 與 OAuth token 兩條路的分流點。"""
        if not token:
            return None

        if token.startswith(API_KEY_PREFIX):
            # 自家 API key —— 完全不碰 OAuth 路徑,行為與 bearer-only 部署一致。
            return await self._api_key_verifier.verify_token(token)

        # OAuth:先讓框架驗它自己簽的 JWT、換回上游 Google token 並驗證。
        access = await super().verify_token(token)
        if access is None:
            return None

        return await self._attach_identity(access)

    async def _attach_identity(self, access: AccessToken) -> AccessToken | None:
        """把 Google 身分換成我們的 user_id + tier + 額度,重新包一顆 AccessToken。"""
        claims = access.claims or {}
        google_sub = claims.get("sub")
        email = claims.get("email")

        if not google_sub:
            # 正常不會發生(GoogleTokenVerifier 沒有 sub 就不會回 token),防呆。
            logger.error("Google token has no 'sub' claim; rejecting")
            return None

        if not email or not _is_email_verified(claims.get("email_verified")):
            # 沒有可信的 email 就對不到我們的帳號 —— 而 email 是這裡唯一的身分錨點
            # (同一個人可能先用 email/密碼註冊過),猜不得,只能拒絕。
            logger.error(
                "Google identity %s has no verified email (email=%r verified=%r); rejecting",
                google_sub,
                email,
                claims.get("email_verified"),
            )
            return None

        identity = await self._resolve_identity(
            google_sub=str(google_sub),
            email=str(email),
            name=claims.get("name"),
            avatar_url=claims.get("picture"),
        )
        if identity is None:
            return None

        quota = await self._quota_cache.state(identity.user_id, identity.tier)
        return AccessToken(
            token=access.token,
            # ⚠️ 一定是**我們的** uuid,不是 Google 的 sub:usage_events.user_id
            # 的外鍵認的是 users.id,填錯每一筆計量都會 INSERT 失敗。
            client_id=identity.user_id,
            scopes=_scopes_for_tier(identity.tier),
            # 沿用框架算好的效期,別把它弄丟 —— 那是 client 判斷何時該 refresh 的依據。
            expires_at=access.expires_at,
            claims={
                # 保留 Google 原本的 claims(sub / email / name / picture …),
                # 再蓋上我們自己的身分與額度資訊。
                **claims,
                "tier": identity.tier,
                "user_id": identity.user_id,
                # OAuth session 沒有 API key。usage_events.api_key_id 可為 NULL,
                # `usage.py` 看到 None 就只記 user 那一半。
                "api_key_id": None,
                "quota_exceeded": quota.exceeded,
                "used_today": quota.used_today,
                "daily_limit": quota.daily_limit,
                "auth_method": "oauth",
            },
        )

    async def _resolve_identity(
        self,
        *,
        google_sub: str,
        email: str,
        name: str | None,
        avatar_url: str | None,
    ) -> _CachedIdentity | None:
        """Google sub → 我們的 user_id + tier(快取優先,必要時 upsert)。

        快取語意刻意抄 bearer 路徑(`auth.py:_resolve`):命中就完全不碰 DB,
        DB 出事時沿用 stale,連 stale 窗口都過了就拒絕 —— 差別只在這裡沒有
        「撤銷」的概念(Google 側撤權會讓上游 token 直接驗不過,輪不到這一層)。
        """
        now = time.monotonic()
        cached = self._identity_cache.get(google_sub)
        if cached is not None and now < cached.fresh_until:
            # 命中 —— 不用每個請求都 upsert 一次(那是一個寫入交易,比查 key 貴)。
            return cached

        try:
            row = await saas_db.upsert_google_user(
                google_sub=google_sub,
                email=email,
                name=name,
                avatar_url=avatar_url,
            )
        except _TRANSIENT_DB_ERRORS as exc:
            if cached is not None and now < cached.stale_until:
                logger.warning(
                    "SaaS DB unavailable (%s); serving stale identity for user %s",
                    exc,
                    cached.user_id,
                )
                return cached
            logger.error("SaaS DB unavailable (%s); rejecting OAuth session", exc)
            self._identity_cache.pop(google_sub, None)
            return None

        if row is None:
            # 這個 email 已經綁在別的 Google 帳號上 —— SQL 層刻意 fail closed,
            # 這裡照著拒絕(理由見 saas_db._UPSERT_GOOGLE_USER_SQL 的註解)。
            logger.error(
                "email %s is already bound to a different Google account; rejecting sub %s",
                email,
                google_sub,
            )
            self._identity_cache.pop(google_sub, None)
            return None

        resolved = _CachedIdentity(
            user_id=str(row["user_id"]),
            tier=str(row["tier"]),
            fresh_until=now + self._identity_cache_ttl,
            stale_until=now + self._identity_stale_ttl,
        )
        remember_entry(self._identity_cache, google_sub, resolved, max_size=CACHE_MAX_SIZE)
        return resolved


def build_oauth_provider(
    *,
    client_id: str,
    client_secret: str,
    public_base_url: str,
    saas_database_url: str,
    cache_ttl: float,
    stale_ttl: float,
    quota_ttl: float,
) -> StockfactsGoogleProvider | None:
    """組出 OAuth provider;設定不全就回 None(呼叫端退回 bearer 模式)。

    刻意不在設定不全時丟例外:少設一個環境變數不該讓整台 server 起不來
    (2026-06-24 的 ETL crash-loop 就是這樣來的),而 `server.py` 有完整的
    fallback 階梯,退到 bearer 模式仍然是一台可用、且不會敞開的 server。

    Args:
        client_id / client_secret: GCP OAuth 2.0 Client 憑證。
        public_base_url: 對外可達的 base URL;Google 的 redirect URI 是
            `<這個值>/auth/callback`(fastmcp 的預設 redirect_path)。
        saas_database_url: SaaS 控制面 DSN,同時給 OAuth 狀態表用。
        cache_ttl / stale_ttl / quota_ttl: 沿用 bearer 路徑的三個 TTL,
            兩條路的快取節奏一致,不必再多三個設定值。
    """
    if not (client_id and client_secret and public_base_url and saas_database_url):
        return None

    # 換掉 fastmcp 的預設同意頁。放在這裡而不是模組載入時:只有真的啟用 OAuth 的
    # 部署才會看到 /consent,bearer-only 或本機 stdio 沒必要動第三方模組。
    # 失敗只是退回預設頁(見 install 的 docstring),不擋 server 起來。
    consent_page.install()

    # OAuth 狀態(client 註冊 / 上游 token / 交易 / code / JTI / refresh metadata)
    # 全部落 Postgres —— pod 換掉、多 replica 都不會掉 session。auto_create=True
    # 讓它自己建表,所以這裡不需要 alembic migration。
    # 注意這是**另一個** asyncpg pool(跟 saas_db 的分開):兩者生命週期與 timeout
    # 需求不同,共用一個 pool 只會讓驗 key 跟 OAuth 寫入互相搶連線。
    store = PostgreSQLStore(url=saas_database_url, table_name=OAUTH_STATE_TABLE)

    # 加密整層儲存(裡面有使用者的上游 Google token,見模組 docstring)。
    # 金鑰從 client secret 決定性推導 → 跨 pod 一致、不必再多一個環境變數。
    encrypted_store = FernetEncryptionWrapper(
        store,
        source_material=client_secret,
        salt=STORAGE_SALT,
        raise_on_decryption_error=False,
    )

    return StockfactsGoogleProvider(
        api_key_verifier=PerUserTokenVerifier(
            cache_ttl=cache_ttl,
            stale_ttl=stale_ttl,
            quota_ttl=quota_ttl,
        ),
        identity_cache_ttl=cache_ttl,
        identity_stale_ttl=stale_ttl,
        client_id=client_id,
        client_secret=client_secret,
        base_url=public_base_url.rstrip("/"),
        required_scopes=GOOGLE_SCOPES,
        client_storage=encrypted_store,
        fastmcp_access_token_expiry_seconds=ACCESS_TOKEN_EXPIRY_S,
        fallback_refresh_token_expiry_seconds=REFRESH_TOKEN_EXPIRY_S,
        # `jwt_signing_key` 刻意不給:不給時 fastmcp 會從 client secret
        # 決定性推導(不是隨機值),跨 pod / 跨 redeploy 自動一致。自己再發明
        # 一個 signing key 環境變數只會多一個會忘記設、設錯就全員登出的東西。
        #
        # `require_authorization_consent` 用預設 True:第一次授權會顯示同意頁。
        # 關掉等於任何註冊過的 client 都能靜默拿到使用者資料,不做。
        #
        # `enable_cimd=False` —— 這台刻意**關掉** CIMD,只留 DCR。
        #
        # CIMD 的成立前提是「授權伺服器抓得到 client 自己託管的 metadata 文件」:
        # client_id 本身是一個 URL,我們得在 /authorize 當下把它 fetch 回來驗。
        # 但實測從這台 pod 抓 Claude Code 的文件
        # (https://claude.ai/oauth/claude-code-client-metadata)一律是
        # `403` + `cf-mitigated: challenge` —— Cloudflare 對這段機房 IP 出的挑戰頁,
        # 換 User-Agent 沒用(跟 Yahoo 擋 Tencent Cloud 整段 IP 是同一類問題)。
        #
        # 開著會比關掉更糟,因為這個旗標同時是**對外公告**:只要 CIMD 沒關,
        # fastmcp 就會在 `/.well-known/oauth-authorization-server` 放
        # `client_id_metadata_document_supported: true`。Claude Code 看到這行就
        # 優先走 CIMD、跳過 DCR,接著在 /authorize 撞上「Client Not Registered」,
        # 而且**不會**自己退回 DCR —— 等於我們宣告了一個自己履行不了的能力,
        # 把主線登入整條堵死。關掉旗標就不公告,client 自然回頭打 /register。
        #
        # DCR(`/register`)反過來不需要任何對外連線,client POST 過來就註冊得成,
        # 在這個網路環境下是唯一可靠的一條;MCP 2026-07-28 spec 雖把它標為
        # deprecated,仍保留至少 12 個月向後相容。哪天出口 IP 不再被 Cloudflare 擋
        # (或架了 egress proxy),把這個參數拿掉就會自動恢復 CIMD。
        enable_cimd=False,
    )
