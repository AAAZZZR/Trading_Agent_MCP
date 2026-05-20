"""FastMCP server 設定 —— 註冊 server identity 與 tools。

Tools 在 `trading_agent_mcp.tools` 透過 `@mcp.tool` 註冊;import 即生效。

Auth:settings.mcp_bearer_token 非空 → 掛 StaticTokenVerifier 在 HTTP transport;
為空 → 不掛(stdio mode 本機開發、或無 auth 的 dev container 用)。
"""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from trading_agent_mcp.settings import settings


def _build_auth() -> StaticTokenVerifier | None:
    """非空 token → StaticTokenVerifier;空 → None(不啟用 auth)。"""
    if not settings.mcp_bearer_token:
        return None
    return StaticTokenVerifier(
        tokens={
            settings.mcp_bearer_token: {
                "client_id": "investor-db-default",
                "scopes": [],
            }
        }
    )


mcp = FastMCP(
    name="investor-db",
    instructions=(
        "美股市場資料查詢 —— 公司基本資料、SEC filings(10-K / 10-Q / 8-K / Form 4)、"
        "財務報表(損益 / 資產負債 / 現金流)、每日股價、內部人交易、機構持股。"
        "資料來源是 investor-db 後端 API。所有金額單位為 USD,日期為 YYYY-MM-DD ISO 格式。"
    ),
    auth=_build_auth(),
)

# 註冊 tools(side effect:tools.py 內的 @mcp.tool 裝飾器跑過會把 tool 掛到 mcp 物件上)
# 放 server 模組底端避免循環 import。
from trading_agent_mcp import tools  # noqa: E402, F401
