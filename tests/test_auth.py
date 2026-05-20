"""server 端 auth 設定的單元測試。

只驗 `_build_auth()` 的條件邏輯與 verifier 對 token 的判斷;HTTP transport
的實際攔截(401 / 200)由 FastMCP 框架負責,框架自己有測試,我們不在這裡覆寫。
"""

from unittest.mock import patch

import pytest
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from trading_agent_mcp.server import _build_auth


def test_no_token_returns_none() -> None:
    """settings.mcp_bearer_token 為空 → 不啟 auth(本機 stdio 開發用)。"""
    with patch("trading_agent_mcp.server.settings") as s:
        s.mcp_bearer_token = ""
        assert _build_auth() is None


def test_token_set_returns_verifier() -> None:
    """非空 token → 回 StaticTokenVerifier。"""
    with patch("trading_agent_mcp.server.settings") as s:
        s.mcp_bearer_token = "test-secret"
        auth = _build_auth()
        assert isinstance(auth, StaticTokenVerifier)


async def test_verifier_accepts_correct_token() -> None:
    with patch("trading_agent_mcp.server.settings") as s:
        s.mcp_bearer_token = "test-secret"
        auth = _build_auth()
        assert auth is not None
        access = await auth.verify_token("test-secret")
        assert access is not None
        assert access.client_id == "investor-db-default"


async def test_verifier_rejects_wrong_token() -> None:
    with patch("trading_agent_mcp.server.settings") as s:
        s.mcp_bearer_token = "test-secret"
        auth = _build_auth()
        assert auth is not None
        assert await auth.verify_token("wrong") is None
        assert await auth.verify_token("") is None
