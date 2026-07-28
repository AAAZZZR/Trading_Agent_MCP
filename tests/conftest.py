"""pytest 設定:在 import trading_agent_mcp 前先把環境變數注入,
避免 settings 載入炸 missing field。所有測試的 base URL 固定為 testing host,
讓 respx 攔截。"""

import os

# 在任何 module import 之前注入,settings = Settings() 才不會 raise。
os.environ.setdefault("MCP_API_BASE_URL", "http://test-api")
os.environ.setdefault("MCP_API_AUTH_TOKEN", "service-token-fixture")
os.environ.setdefault("MCP_BEARER_TOKEN", "client-token-fixture")
