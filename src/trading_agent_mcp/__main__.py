"""`python -m trading_agent_mcp` 入口。

預設 Streamable HTTP transport(對外開,auth 模式見 server.py `_build_auth()`:
per-user / 共用 token / 無 auth)。
本機開發給 Claude Desktop 連時:`python -m trading_agent_mcp --stdio` 切到 stdio。
"""

from __future__ import annotations

import sys

from trading_agent_mcp.server import mcp
from trading_agent_mcp.settings import settings


def main() -> None:
    if "--stdio" in sys.argv:
        # stdio:Claude Desktop 本機 spawn 用;無 auth(本機 process 不對外)。
        mcp.run(transport="stdio")
        return

    # Streamable HTTP:對外服務。Auth 由 FastMCP verifier 處理(見 _build_auth)。
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=settings.port,
    )


if __name__ == "__main__":
    main()
