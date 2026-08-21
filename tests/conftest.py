"""pytest 設定:在 import trading_agent_mcp 前先把環境變數注入,
避免 settings 載入炸 missing field。所有測試的 base URL 固定為 testing host,
讓 respx 攔截。

**一律強制覆寫,不用 `os.environ.setdefault`** —— 外部環境不得洩進測試。
setdefault 的語意是「機器上已經有值就沿用」,等於讓開發機 / CI 的環境變數凌駕
fixture:base URL 被蓋掉 respx 就攔不到,token 被蓋掉 auth 測試就對不上。

其中 `MCP_READONLY_DB_DSN` 最致命:`tests/test_sql_tool.py` 的兩個 no-DSN 測試
(`test_no_dsn_returns_friendly_error` / `test_describe_table_no_dsn_returns_friendly_error`)
靠「`settings.mcp_readonly_db_dsn` 是空字串」才會走 `ReadonlyDBNotConfigured` 那條
friendly-error 路徑。跑測試的機器若真的設了這個變數,那兩個測試會繞過 mock、
**拿真的 DSN 去連生產 Postgres**。所以這裡直接把它清成空字串。
(注意 settings 還會讀 `.env` 檔;環境變數優先序高於 dotenv,設空字串才蓋得掉 ——
只 `pop` 是不夠的。那兩個測試裡另有 `patch.object` 當第二層保險。)
"""

import os

# 在任何 module import 之前注入,settings = Settings() 才不會 raise。
os.environ["MCP_API_BASE_URL"] = "http://test-api"
os.environ["MCP_API_AUTH_TOKEN"] = "service-token-fixture"
os.environ["MCP_BEARER_TOKEN"] = "client-token-fixture"

# settings.py 其餘會被環境影響、且會改變測試行為的欄位,一併釘死在 settings.py 的
# 預設值(乾淨環境下等同沒動,髒環境下才看得出差別)。
os.environ["MCP_PER_USER_AUTH"] = "true"  # 影響 server.py import 期選哪個 verifier
os.environ["MCP_AUTH_CACHE_TTL"] = "300.0"
os.environ["MCP_AUTH_STALE_TTL"] = "3600.0"
os.environ["MCP_QUOTA_CACHE_TTL"] = "60.0"

# 兩個 DSN 一律清空 —— 「沒設 DSN」正是測試要驗的狀態,而且真設了就會連出去:
#   MCP_READONLY_DB_DSN  → execute_readonly_sql / describe_table 的 no-DSN 測試
#   MCP_SAAS_DATABASE_URL → per-user 認證直讀 SaaS DB(測試都自己 patch settings 給值)
# 開發機上這兩個很可能指著真的 Postgres,漏進來就是拿 prod 當測試 fixture。
os.environ["MCP_READONLY_DB_DSN"] = ""
os.environ["MCP_SAAS_DATABASE_URL"] = ""
