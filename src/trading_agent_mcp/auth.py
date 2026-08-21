"""Per-user API-key 認證 —— FastMCP TokenVerifier 子類 + 401 文案改寫。

# 這個模型在解什麼問題

SaaS 模式下每個 user 帶自己的 API key(bearer token),FastMCP 在「每一次」
Streamable HTTP 請求(含 handshake、tools/list、每個 tool 呼叫)都會把該 token
餵進 `verify_token`。舊實作在每一次都同步繞一趟後端 API 去驗 key +
計量,於是:

    MCP → API → DB,任何一段抖動(API redeploy、DB 慢、ingress 掐 keepalive)
    都被翻譯成 401,而 MCP client 對 401 的解讀是「你的憑證壞了」。

使用者明明存了正確的 key,卻動不動被要求重連 / 重建 key。新模型的目標是
**存了 key 就穩**,三個改動:

1. **直接讀 SaaS 控制面 DB**(`saas_db.py`)—— 少一跳就少一段可壞的鏈,而且
   驗 key 本來就只是一個 indexed lookup。
2. **正向快取**(`cache_ttl`,預設 5 分鐘)—— 命中期間完全不碰 DB。
3. **額度不等於壞 key** —— 超額仍然回 AccessToken(連線建得起來),只在
   claims 標記 `quota_exceeded`,由 `usage.py` 的元件層 middleware 在「呼叫
   tool」時回一個看得懂的錯誤。舊模型把超額也回成 401,使用者會以為 key 壞了。

# 快取語意(明文取捨)

- **正向快取 `cache_ttl`**:驗過就在這段時間內不再查 DB。代價是 **key 撤銷
  最多延遲 `cache_ttl` 才生效**。這是刻意的:MCP key 的威脅模型是「使用者自己
  的唯讀資料存取」,晚 5 分鐘失效可以接受;每個請求打一次 DB 換來的脆弱不行。
  相對地,一旦真的查到 `revoked_at` 或查無此 key,會**立刻清掉快取**,不會有
  「撤銷後又被 stale 快取放行」的窗口。
- **stale 沿用 `stale_ttl`**:DB 暫時不可用(重啟 / 網路抖動)時,最近
  `stale_ttl`(預設 60 分鐘)內驗過的 key 仍可續用。沒有快取就回 401 ——
  寧可拒絕也不放行從未驗證過的 key。
- **額度快取 `quota_ttl`**:額度是商業限制不是安全邊界,60 秒的誤差可接受,
  換掉每個請求一次 `count(*)`;查不到時 fail open(見 `_quota`)。

# 與 OAuth 模式的關係(2026-08 新增 `oauth.py` 之後的現況)

本 verifier 自己**不**掛任何 .well-known route(TokenVerifier.get_routes 預設回 []),
所以 bearer-only 部署照舊不對外公告 OAuth metadata,401 就只是「認證失敗」。
但當 `oauth.py` 的 `StockfactsGoogleProvider` 啟用時,metadata 與
authorize / token / register / callback 路由由**它**提供,401 反而是「請去認證」
的正常訊號,client 要靠 401 的 `WWW-Authenticate: ... resource_metadata="..."`
才找得到授權伺服器 —— 這也是 `AuthErrorMessageMiddleware` 必須保留該參數、
並在 OAuth 模式下不改寫文案(`rewrite_description=False`)的原因,見它的 docstring。

FastMCP 自己那句「clear authentication tokens in your MCP client and reconnect」
的 401 文案在 bearer 模式下會誤導使用者刪掉好好的 key,由 `AuthErrorMessageMiddleware`
(ASGI 層)改寫掉。

Tier → scopes 對應在 `_scopes_for_tier`;tool 端用 require_scopes("tier:pro")
做 per-tool gating(見 tools.py)。額度快取抽成 `QuotaCache`,bearer 與 OAuth
兩條認證路徑共用**同一個實例** —— 同一個人換一條路進來,額度是同一份。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import asyncpg
from fastmcp.server.auth import AccessToken, TokenVerifier

from trading_agent_mcp import saas_db
from trading_agent_mcp.saas_db import DEFAULT_TIER, SaasDBNotConfigured

logger = logging.getLogger(__name__)

# tier → 每滾動 24 小時的 MCP tool 呼叫上限(None = 不限)。
# ⚠️ 單一事實來源是 Trading_Agent_API 的 src/investor_db/billing_config.py;
#    這裡是跨 repo 的副本(兩個 repo 不共用套件),改額度要兩邊一起改。
TIER_DAILY_LIMITS: dict[str, int | None] = {"free": 200, "pro": None}

# 「暫時性」DB 錯誤 —— 這幾類代表「現在問不到」,不代表「這把 key 無效」,
# 所以要落回快取而不是把使用者踢成 401:
#   asyncpg.PostgresError —— 伺服器端回的錯(如 too many connections、admin shutdown)
#   asyncpg.InterfaceError —— pool / 連線層的錯(pool 已關、connection is closed);
#                             它不是 PostgresError 的子類,漏掉會整包冒出去
#   OSError               —— TCP / DNS 層斷線(Python 3.11 起 TimeoutError 也是它的子類)
#   asyncio.TimeoutError  —— acquire / query 逾時。3.11+ 等同內建 TimeoutError,
#                             這裡仍明寫出來,讓「逾時」這個意圖不必靠繼承關係去推
#   SaasDBNotConfigured   —— DSN 沒設(設定失誤);同樣不該被誤判成「key 無效」
_TRANSIENT_DB_ERRORS = (
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
    OSError,
    asyncio.TimeoutError,
    SaasDBNotConfigured,
)

# 快取條目上限。長時間執行的 server 若不設限,每把出現過的 key 都會永久留在 dict
# 裡(輪替 / 撤銷過的 key 也不例外),記憶體只增不減。1000 把 key 遠超過實際同時
# 在線的使用者數,夠用;手寫淘汰而不引入 cachetools —— 一個 dict 就能做到,
# 不值得多一個依賴。
CACHE_MAX_SIZE = 1000
QUOTA_CACHE_MAX_SIZE = 1000

# 改寫後的 401 文案。
# ⚠️ 必須是純 ASCII 英文:它會進 `WWW-Authenticate` header,而 HTTP header 值只能
#    latin-1,中文會在 encode 時直接炸掉。
# 內容刻意不叫使用者清掉 / 重新產生 key —— 401 的成因有兩種(key 真的無效 vs
# 帳號 DB 暫時連不上),兩種都不該用「刪掉 key 重來」處理。
AUTH_ERROR_DESCRIPTION = (
    "Authentication failed: this API key could not be verified. It is either "
    "unknown/revoked, or the account database is temporarily unreachable. Check the "
    "key value; if it is correct, retry shortly. Do NOT clear or regenerate your key."
)

# 改寫後要送出的 body / header(內容固定,module 載入時算一次就好)。
_AUTH_ERROR_BODY = json.dumps(
    {"error": "invalid_token", "error_description": AUTH_ERROR_DESCRIPTION}
).encode()
_AUTH_ERROR_WWW_AUTHENTICATE = (
    f'Bearer error="invalid_token", error_description="{AUTH_ERROR_DESCRIPTION}"'
).encode()

# 我們自己重算的 header;原始回應中同名的都要拿掉,免得出現兩個 content-length。
_REPLACED_HEADERS = frozenset({b"content-type", b"content-length", b"www-authenticate"})

# RFC 9728 的 `resource_metadata="<url>"` 參數。MCP client 就是靠 401 上的這個參數
# 找到授權伺服器、進而彈出瀏覽器登入 —— 改寫 `WWW-Authenticate` 時**必須**原樣保留,
# 弄丟它等於把整個 OAuth 探索流程打死(client 收到 401 卻不知道該去哪裡認證)。
_RESOURCE_METADATA_PARAM = re.compile(rb'resource_metadata="[^"]*"')


def remember_entry[CacheValue](
    cache: dict[str, CacheValue],
    key: str,
    value: CacheValue,
    *,
    max_size: int,
) -> None:
    """寫進快取並維持條目上限。

    dict 保有插入序,所以「先 pop 再插入」等於把這一項移到最新端
    (LRU-ish:每次重新驗證都會刷新位置);超出 `max_size` 時從最舊端淘汰。
    被淘汰只是下次請求要重查一次 DB,不影響正確性。

    抽成共用函式是因為現在有三份同構的快取(認證結果 / 額度狀態 /
    `oauth.py` 的 Google 身分),同一段淘汰邏輯抄三次遲早會分岔。
    """
    cache.pop(key, None)
    cache[key] = value
    while len(cache) > max_size:
        cache.pop(next(iter(cache)))


def _scopes_for_tier(tier: str) -> list[str]:
    """把 tier 轉成 OAuth scopes。

    目前一律給 `tier:<tier>` 一個 scope;pro 以上才拿得到 `tier:pro`,
    對應 tools.py 對重量級 tool 的 require_scopes("tier:pro") gating。
    未知 tier 仍給 `tier:<tier>`,讓 free 級 structured tool 可用、但不含 pro scope。
    """
    return [f"tier:{tier}"]


@dataclass(frozen=True)
class _CachedAuth:
    """一把 key 驗證成功後的結果。時間都用 `time.monotonic()`(不受系統時鐘調整影響)。

    fresh_until 之前:直接用,不碰 DB。
    fresh_until ~ stale_until:正常路徑會重查 DB;只有在 DB 出事時才拿來頂著。
    stale_until 之後:視同沒有快取。
    """

    user_id: str
    api_key_id: str
    tier: str
    fresh_until: float
    stale_until: float


@dataclass(frozen=True)
class _CachedQuota:
    """一個 user 的額度狀態快照。`used_today` / `daily_limit` 只是給 client 看的資訊,
    可能為 None(pro 不限額,或查詢失敗時 fail open)。"""

    exceeded: bool
    used_today: int | None
    daily_limit: int | None
    expires_at: float


class QuotaCache:
    """per-user 額度狀態的共用快取 —— bearer 與 OAuth 兩條認證路徑共用**同一個實例**。

    為什麼要共用而不是各自一份:額度是 per-user 的商業限制(`usage_events` 只認
    user_id),同一個人今天用 API key、明天用 Google 登入,計的是同一份 200 次。
    兩條路各留一份快取不但多打一次 `count(*)`,兩份的過期時間還會錯開,
    使用者會看到忽而超額忽而沒超額。

    語意(與模組 docstring 一致):
      - pro(不限額)完全不查 DB —— `count(*)` 是這條路徑上最貴的 query,
        而付費用戶剛好是呼叫最兇的那群,省下來最有價值。
      - 查詢失敗 **fail open**:額度是商業限制不是安全邊界,基礎設施抖動時
        擋住付了錢的使用者,傷害遠大於少計幾次呼叫。fail open 的結果不寫進快取,
        下一個請求會再試一次。

    支援 `in` / `len` 是刻意的:它就是一個有上限的容器,呼叫端(與測試)
    要看「某個 user 有沒有被快取住」時不必去掏內部 dict。
    """

    def __init__(self, *, ttl: float) -> None:
        self.ttl = ttl
        # key = user_id。額度是 per-user 而非 per-key(同一個人多把 key 共用額度)。
        self._cache: dict[str, _CachedQuota] = {}

    def __contains__(self, user_id: object) -> bool:
        return user_id in self._cache

    def __len__(self) -> int:
        return len(self._cache)

    async def state(self, user_id: str, tier: str) -> _CachedQuota:
        """算這個 user 現在超額了沒(快取優先,必要時查 DB)。"""
        limit = TIER_DAILY_LIMITS.get(tier, TIER_DAILY_LIMITS[DEFAULT_TIER])
        now = time.monotonic()

        if limit is None:
            # pro(不限額)—— 連數都不用數。
            return _CachedQuota(exceeded=False, used_today=None, daily_limit=None, expires_at=now)

        cached = self._cache.get(user_id)
        if cached is not None and now < cached.expires_at:
            return cached

        try:
            used = await saas_db.count_usage_24h(user_id)
        except _TRANSIENT_DB_ERRORS as exc:
            logger.warning("quota lookup failed for user %s (%s); failing open", user_id, exc)
            return _CachedQuota(exceeded=False, used_today=None, daily_limit=limit, expires_at=now)

        quota = _CachedQuota(
            exceeded=used >= limit,
            used_today=used,
            daily_limit=limit,
            expires_at=now + self.ttl,
        )
        remember_entry(self._cache, user_id, quota, max_size=QUOTA_CACHE_MAX_SIZE)
        return quota


class PerUserTokenVerifier(TokenVerifier):
    """每個 user 一把 API key —— 直接對 SaaS 控制面 DB 驗證。

    每個 instance 自帶兩份 in-process 快取(認證結果 / 額度狀態),所以是
    per-process 的:多 replica 時各自快取,語意不變(只是各自都會查一次 DB)。
    快取語意與取捨見模組 docstring。
    """

    def __init__(
        self,
        *,
        cache_ttl: float,
        stale_ttl: float,
        quota_ttl: float,
    ) -> None:
        super().__init__()
        self._cache_ttl = cache_ttl
        self._stale_ttl = stale_ttl
        # key = API key 的 sha256 hash(不是明文)。反正查 DB 本來就要算 hash,
        # 順手讓這個長生命週期的 dict 裡不留任何明文憑證。
        self._cache: dict[str, _CachedAuth] = {}
        self._quota_cache = QuotaCache(ttl=quota_ttl)

    @property
    def quota_cache(self) -> QuotaCache:
        """額度快取本體 —— 給 `oauth.py` 取來共用同一份(見 QuotaCache docstring)。"""
        return self._quota_cache

    @property
    def _quota_ttl(self) -> float:
        """建構時傳進來的額度 TTL。實際狀態存在 QuotaCache 裡,這裡不另存一份
        免得兩邊分岔;保留這個名字是為了讓「TTL 有沒有從 settings 帶進來」還看得見。"""
        return self._quota_cache.ttl

    async def verify_token(self, token: str) -> AccessToken | None:
        """驗證 user API key。

        Args:
            token: 來自 Authorization: Bearer 的 user API key。

        Returns:
            有效 → AccessToken(client_id=user_id、scopes 反映 tier);
            無效 / 已撤銷 / 無法驗證 → None(→ 401)。

            注意「超額」**不會**回 None —— 它回 AccessToken 並在 claims 標記
            `quota_exceeded`,讓連線建得起來、只有 tool 呼叫被擋(見 usage.py)。
        """
        if not token:
            return None

        auth = await self._resolve(token)
        if auth is None:
            return None

        quota = await self._quota(auth)
        return AccessToken(
            token=token,
            client_id=auth.user_id,
            scopes=_scopes_for_tier(auth.tier),
            claims={
                "tier": auth.tier,
                "user_id": auth.user_id,
                "api_key_id": auth.api_key_id,
                "quota_exceeded": quota.exceeded,
                "used_today": quota.used_today,
                "daily_limit": quota.daily_limit,
            },
        )

    async def _resolve(self, token: str) -> _CachedAuth | None:
        """把 token 換成 `_CachedAuth`(快取優先,必要時查 DB)。"""
        key_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.monotonic()
        cached = self._cache.get(key_hash)
        if cached is not None and now < cached.fresh_until:
            # 正向快取命中 —— 完全不碰 DB。撤銷最多延遲 cache_ttl 生效,
            # 這是模組 docstring 寫明的取捨。
            return cached

        try:
            row = await saas_db.lookup_key(key_hash)
        except _TRANSIENT_DB_ERRORS as exc:
            if cached is not None and now < cached.stale_until:
                logger.warning(
                    "SaaS DB unavailable (%s); serving stale auth for user %s",
                    exc,
                    cached.user_id,
                )
                return cached
            # 沒快取或連 stale 窗口都過了 —— 不放行未經驗證的 key。
            logger.error("SaaS DB unavailable (%s); rejecting API key", exc)
            self._cache.pop(key_hash, None)
            return None

        if row is None or row["revoked_at"] is not None:
            # 明確失效(查無此 key / 已撤銷)—— 立刻清快取,不留任何 stale 窗口。
            self._cache.pop(key_hash, None)
            return None

        resolved = _CachedAuth(
            user_id=str(row["user_id"]),
            # asyncpg 回的是 UUID 物件;claims 之後會被序列化成 JSON,先轉字串。
            api_key_id=str(row["api_key_id"]),
            tier=str(row["tier"]),
            fresh_until=now + self._cache_ttl,
            stale_until=now + self._stale_ttl,
        )
        remember_entry(self._cache, key_hash, resolved, max_size=CACHE_MAX_SIZE)
        return resolved

    async def _quota(self, auth: _CachedAuth) -> _CachedQuota:
        """算這個 user 現在超額了沒 —— 委派給共用的 QuotaCache(OAuth 路徑用同一個)。"""
        return await self._quota_cache.state(auth.user_id, auth.tier)


class MisconfiguredVerifier(TokenVerifier):
    """設定不全時的「一律拒絕」verifier —— 存在的唯一理由是**不要 fail open**。

    情境:`mcp_per_user_auth=True`(預設值)卻沒給 `mcp_saas_database_url`,
    而且也沒設共用的 `mcp_bearer_token`。這是部署設定失誤,但它不能被翻譯成
    「不啟用 auth」—— 那會讓一台對外服務的 server 完全不設防,任何人都能呼叫
    全部 tool。寧可整台拒收(operator 會立刻發現),也不要默默敞開大門。

    刻意**不**在 import 期 raise:那會變成 crash-loop,連 log 都不容易撈
    (2026-06-24 的 ETL 事故就是這樣來的)。改成照常啟動、每個請求回 401,
    並在建構時 log 一筆點名缺哪個環境變數的 error。
    """

    def __init__(self) -> None:
        super().__init__()
        logger.error(
            "MCP_PER_USER_AUTH is on but MCP_SAAS_DATABASE_URL is empty, and no "
            "MCP_BEARER_TOKEN is set either — every request will be rejected with 401. "
            "Set MCP_SAAS_DATABASE_URL (per-user mode) or MCP_BEARER_TOKEN (shared token)."
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        logger.error("rejecting request: auth is not configured (see startup error)")
        return None


class AuthErrorMessageMiddleware:
    """把 FastMCP 寫死的 401 文案換掉(純 ASGI middleware)。

    為什麼要在 ASGI 層做:那句「clear authentication tokens in your MCP client and
    reconnect」硬寫在 `fastmcp/server/auth/middleware.py` 的
    `RequireAuthMiddleware._send_auth_error`,而 `RequireAuthMiddleware` 是
    `create_streamable_http_app` 直接 new 在 route 上的,auth provider 覆寫不到它。

    但 `mcp.run(..., middleware=[...])` 傳進去的 ASGI middleware 是加在
    `server_middleware` 上、**包在 `RequireAuthMiddleware` 外面**,所以攔得到它送出
    的 response —— 這是唯一不改框架就能改掉那句話的位置。

    文案為什麼要改:那句話等於叫使用者刪掉手上好好的 key。401 的成因有兩種(key
    真的無效 / 帳號 DB 暫時連不上),刪 key 對兩種都沒幫助,對後者還會讓人白白
    重建一把。改成 `AUTH_ERROR_DESCRIPTION`(見上)。

    只碰 401;403 insufficient_scope(tier gating)的文案是對的,原樣放行。

    # `rewrite_description`:OAuth 模式一定要傳 False

    OAuth 啟用之後,401 的意義整個變了 —— 它不再是「你的憑證壞了」,而是
    「請去認證」的正常訊號,框架那句「clear tokens and reconnect」在這個情境下
    描述的**正是 client 該做的事**(去重新拿一次 token)。所以 OAuth 模式下
    傳 `rewrite_description=False`,整個 response 原樣放行,一個 byte 都不動。

    bearer-only 模式維持 True(已上線的行為):那裡沒有 OAuth 可以走,
    那句話只會讓使用者刪掉手上好好的 key。

    ⚠️ 就算在 True 的模式下,`WWW-Authenticate` 裡的 `resource_metadata="..."`
    也一定要原樣接回去(見 `_rewrite_auth_headers`)—— 舊實作把整個 header 換掉,
    一旦哪天同時掛上 OAuth,client 會收到 401 卻找不到授權伺服器,永遠不會彈登入。
    """

    def __init__(self, app: Any, *, rewrite_description: bool = True) -> None:
        self.app = app
        self.rewrite_description = rewrite_description

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not self.rewrite_description:
            await self.app(scope, receive, send)
            return

        # 攔到 401 後,原始回應的 body 事件必須整串丟掉 —— 我們已經自己送了一份
        # 完整的 start + body(而且 content-length 是照新 body 算的),再放行原始
        # body 會讓 client 收到長度對不上的垃圾。
        rewritten = False

        async def send_wrapper(message: Any) -> None:
            nonlocal rewritten
            message_type = message["type"]

            if message_type == "http.response.start":
                if message["status"] != 401:
                    await send(message)
                    return
                rewritten = True
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": _rewrite_auth_headers(message.get("headers") or []),
                    }
                )
                await send({"type": "http.response.body", "body": _AUTH_ERROR_BODY})
                return

            if message_type == "http.response.body" and rewritten:
                return

            await send(message)

        await self.app(scope, receive, send_wrapper)


def _rewrite_auth_headers(headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    """保留原始 header,但換掉 content-type / content-length / www-authenticate。

    唯一從原始 `www-authenticate` 撈回來的是 `resource_metadata="..."`:那是 client
    發現授權伺服器的唯一線索,只要原始回應有帶就一定要接回去(理由見
    `_RESOURCE_METADATA_PARAM`)。
    """
    kept = [(name, value) for name, value in headers if name.lower() not in _REPLACED_HEADERS]
    kept.append((b"content-type", b"application/json"))
    kept.append((b"content-length", str(len(_AUTH_ERROR_BODY)).encode()))

    www_authenticate = _AUTH_ERROR_WWW_AUTHENTICATE
    resource_metadata = _find_resource_metadata(headers)
    if resource_metadata is not None:
        www_authenticate = www_authenticate + b", " + resource_metadata
    kept.append((b"www-authenticate", www_authenticate))
    return kept


def _find_resource_metadata(headers: list[tuple[bytes, bytes]]) -> bytes | None:
    """從原始 header 撈出 `resource_metadata="<url>"`(整個參數,含參數名)。"""
    for name, value in headers:
        if name.lower() != b"www-authenticate":
            continue
        match = _RESOURCE_METADATA_PARAM.search(value)
        if match is not None:
            return match.group(0)
    return None
