"""MCP server 設定 —— 全部從環境變數讀,搬平台只改值不改碼。"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Trading_Agent API base URL(無尾斜線)
    #   本機開發:http://localhost:8000
    #   Zeabur(同 project):http://api.zeabur.internal:8080
    mcp_api_base_url: str

    # Service-to-service bearer:MCP server → Trading_Agent API
    mcp_api_auth_token: str

    # Client bearer:Claude.ai / Desktop → 本 MCP server
    mcp_bearer_token: str

    # Streamable HTTP transport port(Zeabur 會以 $PORT 蓋過)
    port: int = 8000

    # Readonly Postgres DSN —— `execute_readonly_sql` tool 專用,
    # 應指向 `investor_db_readonly` role(scripts/grant_readonly.sql)。
    # asyncpg 用,不要帶 `+asyncpg` driver 前綴:postgresql://user:pw@host:port/db
    # 留空 = 不啟用 SQL tool(其他 tool 不受影響)。
    mcp_readonly_db_dsn: str = ""


settings = Settings()  # type: ignore[call-arg]
