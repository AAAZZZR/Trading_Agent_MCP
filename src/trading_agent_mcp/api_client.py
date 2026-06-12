"""httpx async client 包裝 —— 統一處理 base URL、bearer header、JSON 解析。

對外只暴露單一 module-level singleton `api` —— tools 直接用 `await api.get(...)`。
測試可以 monkeypatch `api._client`(或建立另一份 `APIClient` 注入測試 base_url)。

錯誤翻譯:`get()` 是所有 API-backed tool 的單一咽喉點,所以在這裡把 httpx 的裸
HTTPStatusError / 連線錯誤翻成 FastMCP 的 `ToolError`,訊息「教 agent 下一步」,
這樣 47 個 tool 自動受惠、個別 tool 不必各自處理。
"""

from __future__ import annotations

from typing import Any

import httpx
from fastmcp.exceptions import ToolError

from trading_agent_mcp.settings import settings


def _extract_detail(response: httpx.Response) -> str:
    """從 API 回應 body 取出 JSON `detail`(FastAPI 422/400 的標準錯誤欄位);
    取不到就回空字串,呼叫端自行決定要不要附上。"""
    try:
        body = response.json()
    except (ValueError, httpx.DecodingError):
        return ""
    if isinstance(body, dict):
        detail = body.get("detail")
        if detail is not None:
            return str(detail)
    return ""


def _translate_status_error(exc: httpx.HTTPStatusError) -> ToolError:
    """把 API 的非 2xx 回應翻成「教 agent 下一步」的 ToolError。

    訊息開頭固定帶 `[<status> <method> <path>]` 保留原始 status code 與 path,
    後面接針對該類錯誤的具體指引。
    """
    response = exc.response
    status = response.status_code
    method = response.request.method
    path = response.request.url.path
    prefix = f"[{status} {method} {path}]"

    if status == 404:
        return ToolError(
            f"{prefix} Not found: {path} with {dict(response.request.url.params)}. "
            "If you passed a ticker, verify it with search_companies(q=...) — coverage is "
            "US equities + ADRs only (no indices, no crypto, no non-US listings). "
            "If the ticker is valid, this dataset may not cover it; check get_data_coverage()."
        )

    if status in (400, 422):
        detail = _extract_detail(response)
        detail_part = f" API detail: {detail}." if detail else ""
        return ToolError(
            f"{prefix} Invalid request parameters.{detail_part} "
            "Check parameter formats: dates are YYYY-MM-DD, period is 'annual'|'quarterly', "
            "tickers are uppercase."
        )

    if status in (401, 403):
        return ToolError(
            f"{prefix} Authentication failed between MCP and API (server-side config). "
            "This is not something you can fix by changing parameters; report to the user."
        )

    if status == 429:
        return ToolError(
            f"{prefix} Rate/quota limit hit. Back off and retry once after a pause; "
            "reduce call volume (use limit params)."
        )

    if status >= 500:
        return ToolError(
            f"{prefix} Temporary upstream error. Retry once; if it persists, the dataset may "
            "be mid-refresh — get_data_coverage() shows last successful ingest times."
        )

    # 其餘非 2xx(理論上少見)—— 保留 prefix,給通用提示。
    return ToolError(
        f"{prefix} Unexpected API response. Verify parameters; if it persists, report to the user."
    )


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
        """GET → JSON。

        錯誤一律翻成教學式 `ToolError`(見 `_translate_status_error`):非 2xx 帶
        status/path + 下一步指引;連線逾時 / 連不上另給重試提示。所有 API-backed tool
        共用這個咽喉點,個別 tool 不必各自 try/except。
        """
        try:
            r = await self._client.get(path, params=params)
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _translate_status_error(exc) from exc
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            raise ToolError(
                f"[GET {path}] API unreachable or slow (30s timeout). "
                "Retry once with a smaller limit."
            ) from exc
        return r.json()


api = APIClient()
