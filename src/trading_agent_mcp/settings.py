"""MCP server 設定 —— 全部從環境變數讀,搬平台只改值不改碼。"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Trading_Agent API base URL(無尾斜線)
    #   本機開發:http://localhost:8000
    #   Zeabur(同 project):http://api.zeabur.internal:8080
    mcp_api_base_url: str

    # Service-to-service bearer:MCP server → Trading_Agent API(資料面查詢)。
    mcp_api_auth_token: str

    # Client bearer:Claude.ai / Desktop → 本 MCP server(單一共用 token)。
    # 只在 per-user 模式關閉時使用;per-user 模式下留空即可。
    mcp_bearer_token: str = ""

    # Per-user 認證開關。預設 True(SaaS 生產模式)。
    #   True  → 每個 user 帶自己的 API key,由 PerUserTokenVerifier「直接讀 SaaS
    #           控制面 DB」驗證(api_keys / subscriptions),計量另在 tool 層非同步寫。
    #   False → 退回單一 mcp_bearer_token 的 StaticTokenVerifier(舊行為)。
    # 注意:per-user 模式還需要 mcp_saas_database_url 有值,否則一樣退回 static;
    # stdio 本機開發走 skip_auth,不受此影響。要關掉設環境變數 MCP_PER_USER_AUTH=false。
    mcp_per_user_auth: bool = True

    # SaaS 控制面 DB 的 asyncpg 裸 DSN(postgresql://user:pw@host:port/db,
    # 不要帶 `+asyncpg` 前綴)。per-user 認證的單一資料來源:
    #   - SELECT api_keys / subscriptions —— 驗 key + 判 tier
    #   - SELECT usage_events            —— 算滾動 24 小時用量
    #   - INSERT usage_events            —— 計量
    #   - UPDATE api_keys.last_used_at   —— 「這把 key 最近有在用」
    # ⚠️ 因為要寫,不能用 mcp_readonly_db_dsn 那個唯讀 role —— 兩者用途、權限都不同,
    #    刻意並存(readonly DSN 只給 execute_readonly_sql 這個 tool 用)。
    # 留空 = 停用 per-user 模式(退回 static token)。
    mcp_saas_database_url: str = ""

    # 驗證成功結果的「正向快取」秒數,預設 5 分鐘。
    # 命中期間完全不碰 DB —— 這是本設計最主要的取捨:key 撤銷最多延遲這麼久才生效,
    # 換來的是每個 MCP 請求不再各打一次 DB / API(鏈上任何抖動都曾變成 401)。
    mcp_auth_cache_ttl: float = 300.0

    # DB 不可用時「沿用舊快取」的上限秒數,預設 60 分鐘。
    # 語意:DB 短暫斷線(重啟 / 網路抖動)時,最近一小時內驗過的 key 仍可續用,
    # agent 的長工作階段不會整批被踢掉;超過就回 401(不放行無法驗證的 key)。
    mcp_auth_stale_ttl: float = 3600.0

    # 額度狀態(用了幾次 / 是否超額)的快取秒數,預設 60 秒。
    # 額度是商業限制不是安全邊界,60 秒的誤差可接受,換掉每個請求一次 count(*)。
    mcp_quota_cache_ttl: float = 60.0

    # ------------------------------------------------------------------
    # OAuth 2.1(Google 上游)—— 主線認證路徑
    #
    # 這三個 + `mcp_saas_database_url` 四者**齊全**才啟用 OAuth;缺任何一個就
    # 靜靜退回 bearer API key 模式(見 server.py `_build_auth()`),**不 crash** ——
    # 少設一個環境變數就讓整台 server 起不來,是比「少一個功能」糟得多的失敗模式。
    #
    # 刻意只有三個變數:
    #   - 簽 FastMCP JWT 的 signing key 由 client secret **決定性推導**
    #     (fastmcp `OAuthProxy`:derive_jwt_key(salt="fastmcp-jwt-signing-key")),
    #     跨 pod / 跨 redeploy 自動一致,不需要也不該再多一個 env。
    #   - OAuth 狀態儲存的加密金鑰同理,由 client secret 推導(見 oauth.py)。
    # 也就是說:輪替 Google client secret 會讓既有的 OAuth session 全部失效
    # (使用者重新授權一次即可),這是刻意的取捨 —— 換來零額外密鑰管理。
    # ------------------------------------------------------------------

    # 對外可達的 base URL(無尾斜線),例如 https://mcp.livermore.club。
    # OAuth 的 issuer、metadata 位址與 Google redirect URI 全部由它推導;
    # redirect URI = <這個值>/auth/callback,必須逐字加進 GCP OAuth client 的
    # 「Authorized redirect URIs」,否則 Google 會拒絕整個授權流程。
    mcp_public_base_url: str = ""

    # GCP OAuth 2.0 Client(類型 Web application)的 client id / secret。
    mcp_google_client_id: str = ""
    mcp_google_client_secret: str = ""

    # Streamable HTTP transport port(Zeabur 會以 $PORT 蓋過)
    port: int = 8000

    # Readonly Postgres DSN —— `execute_readonly_sql` tool 專用,
    # 應指向 `investor_db_readonly` role(scripts/grant_readonly.sql)。
    # asyncpg 用,不要帶 `+asyncpg` driver 前綴:postgresql://user:pw@host:port/db
    # 留空 = 不啟用 SQL tool(其他 tool 不受影響)。
    mcp_readonly_db_dsn: str = ""


settings = Settings()  # type: ignore[call-arg]
