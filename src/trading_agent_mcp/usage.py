"""用量計量 —— FastMCP 元件層 middleware。

舊模型把計量綁在 auth 上:每個 HTTP 請求(handshake、tools/list、每次 tool 呼叫)
都同步 POST 一次後端才放行,於是計量的失敗 = 認證的失敗 = 使用者被踢。這裡把兩件
事拆開:

- **粒度**:改成「每次 tool 呼叫」記一筆。順帶拿到真正的 tool 名稱 —— 框架驗
  token 時還沒 dispatch,舊模型只能一律記成 "connect"。
- **時機**:fire-and-forget。計量是事後帳,不該擋在使用者與資料之間;寫失敗只
  記 log,tool 照常執行。
- **超額**:在這裡擋,而不是在 auth 擋。連線照常建立(key 是好的),只有 tool
  呼叫回一個看得懂的 ToolError,不會讓 client 以為憑證壞掉要重連。
"""

from __future__ import annotations

import asyncio
import logging

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from mcp.types import CallToolRequestParams

from trading_agent_mcp import saas_db

logger = logging.getLogger(__name__)

# 超額訊息。用英文跟 tools.py 既有的 ToolError 一致(agent 讀的是這串)。
# 刻意不寫「明天重置」—— 額度視窗是滾動 24 小時,最舊的呼叫滿 24 小時就自動釋出。
# 最後一句是重點:別讓 agent 建議使用者去重建 key。
QUOTA_EXCEEDED_MESSAGE = (
    "Daily quota reached: the Free plan allows 200 tool calls per rolling 24 hours "
    "and you have used them all. Capacity frees up automatically as your oldest calls "
    "age past 24 hours, or upgrade to Pro for unlimited calls. Your API key is still "
    "valid - no need to reconnect or regenerate it."
)

# 已送出但還沒跑完的計量 task。
# ⚠️ 一定要留這個強引用:asyncio 對 task 只保有弱引用,沒人拿著的 task 可能在跑完
#    之前就被 GC 掉,計量會隨機消失(且不會有任何錯誤訊息)。跑完再從 set 移除。
_pending_writes: set[asyncio.Task[None]] = set()


async def _record(user_id: str, api_key_id: str | None, action: str) -> None:
    """背景寫一筆用量。所有例外都在這裡吞掉 —— 這是 fire-and-forget task,
    往外冒只會變成 asyncio 的 "Task exception was never retrieved" 噪音,
    而且計量失敗絕不該影響已經在跑的 tool。"""
    try:
        await saas_db.record_usage(user_id=user_id, api_key_id=api_key_id, action=action)
    except Exception:
        logger.warning("usage metering failed (user=%s action=%s)", user_id, action, exc_info=True)


class UsageMiddleware(Middleware):
    """每次 tool 呼叫:先擋超額,再非同步記一筆用量。

    只在「claims 帶得出 user_id」的兩種模式下掛(OAuth / per-user bearer,見
    server.py)—— static token / 無 auth 模式的 client_id 不是 uuid,硬記會每次
    INSERT 都失敗刷 log。
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        token = get_access_token()
        if token is None:
            # 沒有 auth(stdio 本機開發 / 無 verifier)—— 沒有 user 可以記。
            return await call_next(context)

        claims = token.claims or {}
        if claims.get("quota_exceeded"):
            # 額度用完 —— 擋這一次呼叫,連線與其他 MCP 操作不受影響。
            raise ToolError(QUOTA_EXCEEDED_MESSAGE)

        user_id = claims.get("user_id")
        api_key_id = claims.get("api_key_id")
        if user_id:
            # 有 user_id 就計量。**api_key_id 可以是 None** —— OAuth session 根本沒有
            # API key,而額度是 per-user 的,少記這些呼叫等於白送 OAuth 使用者無限額度。
            # (usage_events.api_key_id 本來就 nullable,見 saas_db.record_usage。)
            #
            # 計量發在 call_next「之前」:記的是「呼叫嘗試」,與舊模型每請求計量
            # 的口徑一致(tool 執行失敗也算一次,否則失敗的重試會變成免費額度)。
            task = asyncio.create_task(
                _record(
                    str(user_id),
                    str(api_key_id) if api_key_id else None,
                    context.message.name,
                )
            )
            _pending_writes.add(task)
            task.add_done_callback(_pending_writes.discard)

        return await call_next(context)
