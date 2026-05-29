"""MCP server 設定 —— 全部從環境變數讀,搬平台只改值不改碼。"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Trading_Agent API base URL(無尾斜線)
    #   本機開發:http://localhost:8000
    #   Zeabur(同 project):http://api.zeabur.internal:8080
    mcp_api_base_url: str

    # Service-to-service bearer:MCP server → Trading_Agent API
    #   per-user 模式下也拿來打 /api/mcp/authorize(驗證 user key)。
    mcp_api_auth_token: str

    # Client bearer:Claude.ai / Desktop → 本 MCP server(單一共用 token)。
    # 只在 per-user 模式關閉時使用;per-user 模式下留空即可。
    mcp_bearer_token: str = ""

    # Per-user 認證開關。預設 True(SaaS 生產模式)。
    #   True  → 每個 user 帶自己的 API key,經 PerUserTokenVerifier 打
    #           /api/mcp/authorize 驗證 + 計量(SaaS 模式)。
    #   False → 退回單一 mcp_bearer_token 的 StaticTokenVerifier(舊行為)。
    # 注意:per-user 模式還需要 mcp_api_base_url 有值,否則一樣退回 static;
    # stdio 本機開發走 skip_auth,不受此影響。要關掉設環境變數 MCP_PER_USER_AUTH=false。
    mcp_per_user_auth: bool = True

    # PerUserTokenVerifier 的 in-process 快取 TTL(秒)。
    # 只快取「validity / tier」做為 authorize 短暫失敗時的 fallback,
    # 不取代每次 POST(計量與配額仍以後端為準)。詳見 auth.py。
    mcp_authorize_cache_ttl: float = 20.0

    # Streamable HTTP transport port(Zeabur 會以 $PORT 蓋過)
    port: int = 8000

    # Readonly Postgres DSN —— `execute_readonly_sql` tool 專用,
    # 應指向 `investor_db_readonly` role(scripts/grant_readonly.sql)。
    # asyncpg 用,不要帶 `+asyncpg` driver 前綴:postgresql://user:pw@host:port/db
    # 留空 = 不啟用 SQL tool(其他 tool 不受影響)。
    mcp_readonly_db_dsn: str = ""


settings = Settings()  # type: ignore[call-arg]
