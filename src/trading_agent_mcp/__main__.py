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
    #
    # stateless_http=True:每個請求自成一體,server 端不保存 MCP session。
    # 有狀態模式下 client 會拿著 Mcp-Session-Id 回來,而該 session 只存在於「當初
    # 建立它的那個 process」的記憶體裡 —— redeploy 換 pod、或日後擴到多 replica,
    # client 的舊 session id 就會撲空(HTTP 404 / 需重新握手)。無狀態模式沒有黏著
    # 問題,任何 replica 都能接任何請求;代價是不支援 server→client 的主動推送
    # (resumability / SSE 續傳),本 server 全是 request-response 的 tool 呼叫,用不到。
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=settings.port,
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
