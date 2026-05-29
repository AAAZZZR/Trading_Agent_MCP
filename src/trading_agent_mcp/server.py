"""FastMCP server 設定 —— 註冊 server identity 與 tools。

Tools 在 `trading_agent_mcp.tools` 透過 `@mcp.tool` 註冊;import 即生效。

Auth(`_build_auth` 決定,優先序由上到下):
  1. per-user 模式(mcp_per_user_auth=True 且 mcp_api_base_url 有值)
     → PerUserTokenVerifier:每個 user 帶自己的 API key,打後端 authorize 驗證 + 計量。
  2. 單一共用 token(mcp_bearer_token 非空)→ StaticTokenVerifier(舊行為 / 本機)。
  3. 兩者皆無 → None(不啟用 auth;stdio 本機開發或無 auth 的 dev container)。
"""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from trading_agent_mcp.auth import PerUserTokenVerifier
from trading_agent_mcp.settings import settings


def _build_auth() -> TokenVerifier | None:
    """依 settings 決定 verifier;見模組 docstring 的優先序。"""
    # 1. Per-user SaaS 模式 —— 需要 base URL 才能打 authorize,否則退回 static。
    if settings.mcp_per_user_auth and settings.mcp_api_base_url:
        return PerUserTokenVerifier(
            api_base_url=settings.mcp_api_base_url,
            service_token=settings.mcp_api_auth_token,
            cache_ttl=settings.mcp_authorize_cache_ttl,
        )

    # 2. 單一共用 token(向後相容:本機 / 舊部署)。
    #    共用 token = 完整權限,給 tier:pro scope 讓重量級 tool(execute_readonly_sql)
    #    在非 per-user 模式下照常可用(per-tool gating 在 per-user 模式才區分 tier)。
    if settings.mcp_bearer_token:
        return StaticTokenVerifier(
            tokens={
                settings.mcp_bearer_token: {
                    "client_id": "investor-db-default",
                    "scopes": ["tier:pro"],
                }
            }
        )

    # 3. 不啟用 auth。
    return None


mcp = FastMCP(
    name="investor-db",
    instructions=(
        "美股市場資料查詢 —— 公司基本資料與搜尋、SEC filings(10-K / 10-Q / 8-K / Form 4)、"
        "財務報表(損益 / 資產負債 / 現金流)、每日 / 每小時股價、內部人交易與跨市場篩選、"
        "機構持股(yfinance 概覽 + 第一手 SEC 13F)。資料來源是 investor-db 後端 API。"
        "所有金額單位為 USD,日期為 YYYY-MM-DD ISO 格式。\n"
        "選工具:不知道精確 ticker 用 search_companies;要彈性 / 統計查詢用 "
        "execute_readonly_sql,下手前先用 describe_table 看欄位(兩者需 pro tier)。"
    ),
    auth=_build_auth(),
)

# 註冊 tools(side effect:tools.py 內的 @mcp.tool 裝飾器跑過會把 tool 掛到 mcp 物件上)
# 放 server 模組底端避免循環 import。
from trading_agent_mcp import tools  # noqa: E402, F401
