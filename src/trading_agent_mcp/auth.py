"""Per-user API-key 認證 —— FastMCP TokenVerifier 子類。

SaaS 模式下每個 user 帶自己的 API key(bearer token),FastMCP 在「每一次」
Streamable HTTP 請求(含 handshake)都會把該 token 餵進 `verify_token`。
我們拿 token 打後端 `POST /api/mcp/authorize` 驗證 + 計量:

  - 帶 service token(settings.mcp_api_auth_token)當 Authorization。
  - body:{"key": <user_api_key>, "action": "connect", "surface": "mcp"}
    (action 固定 "connect" —— 見下方說明)
  - 後端永遠回 HTTP 200:
      ok=true  → {ok, user_id, tier, used_today, daily_limit}
      ok=false → {ok, reason: invalid_key|revoked|quota_exceeded, ...}

ok=true → 回 AccessToken(client_id=user_id、scopes 反映 tier);
ok=false / HTTP error → 回 None → FastMCP 回 401。

因為本 verifier 不掛任何 .well-known route(TokenVerifier.get_routes 預設回 []),
不會對外公告 OAuth metadata,所以 Claude Code 看到 401 會直接顯示認證失敗,
不會誤入 OAuth 流程。

關於 action 欄位:FastMCP 框架在 transport 層呼叫 `verify_token(token)` 時「只」
給 token —— 此時尚未 dispatch 到具體 tool,框架不知道 tool 名稱。因此每一次 MCP
請求(handshake、列 tool、呼叫 tool)在後端都記為 action="connect"。後端仍對每次
authorize 呼叫計量 + enforce 配額,只是粒度是「每次 MCP 請求」而非「每個 tool 名稱」。
Per-tool 的權限差異改用 scopes(tier:pro)在 FastMCP 元件層 gating,不靠 action 字串。

Tier → scopes 對應在 `_scopes_for_tier`;tool 端用 require_scopes("tier:pro")
做 per-tool gating(見 tools.py)。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
from fastmcp.server.auth import AccessToken, TokenVerifier

# action 欄位在「連線握手」階段(還沒呼叫任何 tool)送的值。
CONNECT_ACTION = "connect"

# 後端 authorize endpoint 的相對路徑。
AUTHORIZE_PATH = "/api/mcp/authorize"

# fallback 快取的條目上限。長時間執行的 server 若不設限,每把出現過的 key 都會
# 永久留在 dict 裡(輪替 / 撤銷過的 key 也不例外),記憶體只增不減。1000 把 key
# 遠超過實際同時在線的使用者數,夠用;手寫淘汰而不引入 cachetools —— 一個 dict
# 就能做到,不值得多一個依賴。
CACHE_MAX_SIZE = 1000


def _scopes_for_tier(tier: str) -> list[str]:
    """把後端回的 tier 轉成 OAuth scopes。

    目前一律給 `tier:<tier>` 一個 scope;pro 以上才拿得到 `tier:pro`,
    對應 tools.py 對重量級 tool 的 require_scopes("tier:pro") gating。
    未知 tier 仍給 `tier:<tier>`,讓 free 級 structured tool 可用、但不含 pro scope。
    """
    return [f"tier:{tier}"]


@dataclass(frozen=True)
class _CachedAuth:
    """快取項目 —— 只存 validity / tier,不存配額數字(配額以後端為準)。"""

    user_id: str
    tier: str
    expires_at: float  # monotonic 時間;超過就視為過期


class PerUserTokenVerifier(TokenVerifier):
    """每個 user 一把 API key —— 打後端 authorize 驗證 + 計量。

    快取取捨(重要):
      後端 authorize 同時負責「計量 + 配額」,所以正確性要求我們「每次請求都 POST」,
      不能用快取跳過呼叫。因此這裡的 in-process TTL 快取「只」用於 authorize 呼叫
      「短暫失敗」(網路抖動 / 5xx / timeout)時的 fallback:TTL 內若先前驗證過、
      且非「明確失效(invalid_key / revoked / quota_exceeded)」,就放行並沿用上次的 tier,
      避免一個 session 中途因暫時性錯誤被踢掉。

      取捨結論:正確性(計量 + 配額)> 微優化。配額永遠由後端在每次成功呼叫時即時
      enforce;快取只在後端「暫時聯絡不上」時提供有限的韌性,不會放行「已知失效」的 key。

      寬限期預設 30 分鐘(settings.mcp_authorize_cache_ttl):後端 redeploy / 短暫
      5xx 時,最近半小時內驗過的 key 仍可續用,agent 的長工作階段不會整批被踢。
      條目上限 CACHE_MAX_SIZE,超過從最舊端淘汰,避免長跑 process 記憶體只增不減。
    """

    def __init__(
        self,
        api_base_url: str,
        service_token: str,
        cache_ttl: float = 1800.0,
    ) -> None:
        super().__init__()
        # 共用一個 AsyncClient(connection pool);service token 直接烘進 header,
        # base_url 去尾斜線避免雙斜線。
        #
        # 自訂 transport 的兩個理由:
        #   retries=2      —— 連線層(connect / DNS)的瞬斷自動重試,不必讓一次
        #                     TCP 抖動就把使用者踢成 401。注意這只重試「建立連線」
        #                     失敗,已送出的 POST 不會重送(計量不會重複計)。
        #   limits         —— keepalive 連線池:authorize 每個請求都打一次後端,
        #                     維持長連線省掉 TCP + TLS handshake。
        # ⚠️ limits 必須傳給 transport:AsyncClient(limits=...) 只在「用預設
        #    transport」時生效,一旦帶自訂 transport 就會被整個蓋掉、形同沒設。
        self._client = httpx.AsyncClient(
            base_url=api_base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {service_token}"},
            timeout=10.0,
            transport=httpx.AsyncHTTPTransport(
                retries=2,
                limits=httpx.Limits(
                    max_keepalive_connections=10,
                    keepalive_expiry=30.0,
                ),
            ),
        )
        self._cache_ttl = cache_ttl
        # token → 上次成功驗證的結果。只在 authorize 暫時失敗時當 fallback。
        self._cache: dict[str, _CachedAuth] = {}

    async def aclose(self) -> None:
        """關掉底層 httpx client(shutdown / 測試用)。"""
        await self._client.aclose()

    async def verify_token(
        self, token: str, action: str = CONNECT_ACTION
    ) -> AccessToken | None:
        """驗證 user API key。

        Args:
            token: 來自 Authorization: Bearer 的 user API key。
            action: 回報給後端計量的動作字串。FastMCP 框架呼叫時「只」給 token,
                    所以實務上一律是預設值 "connect";保留此參數是為了單元測試
                    與未來可能的中介層能傳入更細的動作。

        Returns:
            ok=true → AccessToken;ok=false / 解析錯誤 → None(→ 401)。
            authorize 暫時失敗(網路 / 5xx)時,TTL 內有快取則沿用,否則回 None。
        """
        if not token:
            return None

        try:
            resp = await self._client.post(
                AUTHORIZE_PATH,
                json={"key": token, "action": action, "surface": "mcp"},
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            # 網路 / 非 2xx / JSON parse 失敗 —— 視為暫時性,嘗試快取 fallback。
            # 明確的「失效」是後端用 ok=false(HTTP 200)表達,不會走到這裡。
            return self._fallback_from_cache(token, exc)

        if data.get("ok") is True:
            user_id = str(data.get("user_id", "unknown"))
            tier = str(data.get("tier", "free"))
            self._remember(token, user_id, tier)
            return AccessToken(
                token=token,
                client_id=user_id,
                scopes=_scopes_for_tier(tier),
                # claims 保留後端原始回應,方便 tool 端讀 used_today / daily_limit。
                claims={
                    "tier": tier,
                    "used_today": data.get("used_today"),
                    "daily_limit": data.get("daily_limit"),
                },
            )

        # ok=false —— 明確失效(invalid_key / revoked / quota_exceeded)。
        # 清掉任何快取,確保不會用舊的 validity 放行已失效的 key。
        self._cache.pop(token, None)
        return None

    def _remember(self, token: str, user_id: str, tier: str) -> None:
        """把成功驗證的結果寫進 fallback 快取,並維持條目上限。

        dict 保有插入序,所以「先 pop 再插入」等於把這把 key 移到最新端
        (LRU-ish:每次成功驗證都會刷新位置);超出 CACHE_MAX_SIZE 時從最舊端
        淘汰。淘汰只影響「後端暫時失敗時能不能沿用」,不影響正常路徑
        (正常路徑一律重打 authorize)。
        """
        self._cache.pop(token, None)
        self._cache[token] = _CachedAuth(
            user_id=user_id,
            tier=tier,
            expires_at=time.monotonic() + self._cache_ttl,
        )
        while len(self._cache) > CACHE_MAX_SIZE:
            self._cache.pop(next(iter(self._cache)))

    def _fallback_from_cache(
        self, token: str, exc: Exception
    ) -> AccessToken | None:
        """authorize 暫時失敗時的 fallback:TTL 內有快取就沿用上次 tier。"""
        cached = self._cache.get(token)
        if cached is None or cached.expires_at <= time.monotonic():
            # 沒快取或已過期 —— 不放行(寧可 401 也不放行未經驗證的 key)。
            self._cache.pop(token, None)
            return None
        # 沿用上次成功的 user_id / tier;配額正確性已知有風險,但僅限 TTL 窗口。
        return AccessToken(
            token=token,
            client_id=cached.user_id,
            scopes=_scopes_for_tier(cached.tier),
            claims={"tier": cached.tier, "stale": True, "error": str(exc)},
        )
