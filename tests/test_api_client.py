"""api_client.py 的單元測試 —— bearer header 注入、教學式錯誤翻譯。"""

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

from trading_agent_mcp.api_client import APIClient


async def test_get_attaches_bearer_header() -> None:
    """每次 GET 都帶 Authorization: Bearer <token>(service-to-service auth)。"""
    client = APIClient(base_url="http://test-api", auth_token="my-secret-token")

    with respx.mock(base_url="http://test-api") as mock:
        route = mock.get("/api/companies").respond(200, json=[{"ticker": "AAPL"}])
        result = await client.get("/api/companies")

    assert result == [{"ticker": "AAPL"}]
    assert route.called
    assert route.calls.last.request.headers["Authorization"] == "Bearer my-secret-token"
    await client.close()


async def test_get_passes_params() -> None:
    client = APIClient(base_url="http://test-api", auth_token="t")
    with respx.mock(base_url="http://test-api") as mock:
        route = mock.get("/api/prices").respond(200, json=[])
        await client.get("/api/prices", params={"ticker": "AAPL", "limit": 10})

    assert route.called
    sent = route.calls.last.request
    assert sent.url.params["ticker"] == "AAPL"
    assert sent.url.params["limit"] == "10"
    await client.close()


async def test_404_translated_to_teaching_tool_error() -> None:
    """404 → ToolError,訊息帶 status/path 前綴並教 agent 用 search_companies / 覆蓋範圍。"""
    client = APIClient(base_url="http://test-api", auth_token="t")
    with respx.mock(base_url="http://test-api") as mock:
        mock.get("/api/companies/XYZZY").respond(404, json={"detail": "Unknown ticker"})
        with pytest.raises(ToolError) as excinfo:
            await client.get("/api/companies/XYZZY")

    msg = str(excinfo.value)
    assert msg.startswith("[404 GET /api/companies/XYZZY]")
    assert "search_companies" in msg
    assert "get_data_coverage()" in msg
    await client.close()


async def test_422_includes_api_detail() -> None:
    """422 → ToolError,附上 API 回的 detail + 參數格式提示。"""
    client = APIClient(base_url="http://test-api", auth_token="t")
    with respx.mock(base_url="http://test-api") as mock:
        mock.get("/api/prices/daily").respond(
            422, json={"detail": "start must be before end"}
        )
        with pytest.raises(ToolError) as excinfo:
            await client.get("/api/prices/daily", params={"ticker": "AAPL"})

    msg = str(excinfo.value)
    assert msg.startswith("[422 GET /api/prices/daily]")
    assert "start must be before end" in msg
    assert "YYYY-MM-DD" in msg
    await client.close()


async def test_5xx_translated_with_retry_hint() -> None:
    """5xx → ToolError,提示重試一次並指向 get_data_coverage 看刷新時間。"""
    client = APIClient(base_url="http://test-api", auth_token="t")
    with respx.mock(base_url="http://test-api") as mock:
        mock.get("/api/companies").respond(500, text="boom")
        with pytest.raises(ToolError) as excinfo:
            await client.get("/api/companies")

    msg = str(excinfo.value)
    assert msg.startswith("[500 GET /api/companies]")
    assert "Retry once" in msg
    assert "get_data_coverage()" in msg
    await client.close()


async def test_timeout_translated_to_tool_error() -> None:
    """連線逾時 → ToolError,提示縮小 limit 重試;不外洩裸 httpx 例外。"""
    client = APIClient(base_url="http://test-api", auth_token="t")
    with respx.mock(base_url="http://test-api") as mock:
        mock.get("/api/companies").mock(
            side_effect=httpx.TimeoutException("timed out")
        )
        with pytest.raises(ToolError) as excinfo:
            await client.get("/api/companies")

    msg = str(excinfo.value)
    assert msg.startswith("[GET /api/companies]")
    assert "Retry once" in msg
    await client.close()
