"""api_client.py 的單元測試 —— bearer header 注入、錯誤 propagation。"""

import httpx
import pytest
import respx

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


async def test_non_2xx_raises() -> None:
    """API 回 4xx / 5xx 時 raise httpx.HTTPStatusError —— FastMCP 會轉成 tool error。"""
    client = APIClient(base_url="http://test-api", auth_token="t")
    with respx.mock(base_url="http://test-api") as mock:
        mock.get("/api/companies/MISSING").respond(404, json={"detail": "Unknown ticker"})
        with pytest.raises(httpx.HTTPStatusError):
            await client.get("/api/companies/MISSING")
    await client.close()
