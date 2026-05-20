"""httpx async client 包裝 —— 統一處理 base URL、bearer header、JSON 解析。

對外只暴露單一 module-level singleton `api` —— tools 直接用 `await api.get(...)`。
測試可以 monkeypatch `api._client`(或建立另一份 `APIClient` 注入測試 base_url)。
"""

from __future__ import annotations

from typing import Any

import httpx

from trading_agent_mcp.settings import settings


class APIClient:
    """Trading_Agent API 的 async client(共用一個 httpx.AsyncClient)。"""

    def __init__(self, base_url: str | None = None, auth_token: str | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url or settings.mcp_api_base_url,
            headers={"Authorization": f"Bearer {auth_token or settings.mcp_api_auth_token}"},
            timeout=30.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET → JSON。非 2xx 抛 httpx.HTTPStatusError,FastMCP 會轉成 tool error。"""
        r = await self._client.get(path, params=params)
        r.raise_for_status()
        return r.json()


api = APIClient()
