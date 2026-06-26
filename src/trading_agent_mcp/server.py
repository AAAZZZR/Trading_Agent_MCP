"""FastMCP server 設定 —— 註冊 server identity 與 tools。

Tools 在 `trading_agent_mcp.tools` 透過 `@mcp.tool` 註冊;import 即生效。

Auth(`_build_auth` 決定,優先序由上到下):
  1. per-user 模式(mcp_per_user_auth=True 且 mcp_api_base_url 有值)
     → PerUserTokenVerifier:每個 user 帶自己的 API key,打後端 authorize 驗證 + 計量。
  2. 單一共用 token(mcp_bearer_token 非空)→ StaticTokenVerifier(舊行為 / 本機)。
  3. 兩者皆無 → None(不啟用 auth;stdio 本機開發或無 auth 的 dev container)。
"""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from trading_agent_mcp.auth import PerUserTokenVerifier
from trading_agent_mcp.settings import settings


def _build_auth() -> TokenVerifier | None:
    """依 settings 決定 verifier;見模組 docstring 的優先序。"""
    # 1. Per-user SaaS 模式 —— 需要 base URL 才能打 authorize,否則退回 static。
    if settings.mcp_per_user_auth and settings.mcp_api_base_url:
        return PerUserTokenVerifier(
            api_base_url=settings.mcp_api_base_url,
            service_token=settings.mcp_api_auth_token,
            cache_ttl=settings.mcp_authorize_cache_ttl,
        )

    # 2. 單一共用 token(向後相容:本機 / 舊部署)。
    #    共用 token = 完整權限,給 tier:pro scope 讓重量級 tool(execute_readonly_sql)
    #    在非 per-user 模式下照常可用(per-tool gating 在 per-user 模式才區分 tier)。
    if settings.mcp_bearer_token:
        return StaticTokenVerifier(
            tokens={
                settings.mcp_bearer_token: {
                    "client_id": "investor-db-default",
                    "scopes": ["tier:pro"],
                }
            }
        )

    # 3. 不啟用 auth。
    return None


mcp = FastMCP(
    name="investor-db",
    instructions=(
        "美股市場資料查詢 —— 公司基本資料與搜尋、SEC filings(10-K / 10-Q / 8-K / Form 4)、"
        "財務報表(損益 / 資產負債 / 現金流)、估值快照(市值 / 本益比家族 / 均線)、"
        "第一手日 K(~5 年)/ 每小時股價、內部人交易與跨市場篩選、"
        "機構持股(持股比例概覽 + 第一手 SEC 13F + filer 反查)、公司行動(現金股息 / 股票分割)、"
        "財報行事曆(單一公司 + 跨市場區間掃描)、法說會逐字稿(管理層語氣 / 每段情緒)、"
        "新聞 + 情緒(vendor 聚合,單一公司 + 全市場)、"
        "ETF 透視(概況 / 成分股 / 類股權重 / 反查持有它的 ETF)、"
        "期權鏈(EOD 報價 / IV / greeks)、總經 / 商品 / 指數 時間序列,以及兩個整合視角:"
        "四面向紅綠燈分析(get_analysis,帶解讀)與客觀數據包(get_objective_report,純數據)。"
        "資料來源是 investor-db 後端 API。"
        "金額單位為 USD,日期為 YYYY-MM-DD ISO 格式;例外:ETF weight 與費用率 / 配息率為 "
        "0-1 小數(非百分比),macro 觀測值單位逐 series 不同,須查 list_macro_series 的 unit 欄位。\n"
        "所有資料皆為 EOD 或更慢、無即時報價;分析師資料僅限兩個 vendor 欄位"
        "(earnings_calendar.estimate_eps、overview.analyst_target_price,引用須註明出處),"
        "無評等 / 營收預測 / 預估修正史。"
        "引用精確數字給使用者前,先用 get_data_coverage 確認該領域的新鮮度與覆蓋範圍。"
        "**凡是工具查得到的數字,一律用工具查,不得用你的訓練記憶回答**(記憶裡的市場數據"
        "必然過時);工具查不到就如實說查不到,不要引導使用者去外部網站。\n"
        "選工具:不知道精確 ticker 用 search_companies;要快速結論用 get_analysis,要自己分析的原料用 "
        "get_objective_report;查 macro 觀測值前先用 list_macro_series 拿 series_id 與單位;"
        "要彈性 / 統計查詢用 execute_readonly_sql,下手前先用 describe_table 看欄位(兩者需 pro tier)。\n"
        "另有資料字典 resource(data://dictionary,各表單位 / 調整 / 代碼 / 滯後語意)、HTML 報告模板 "
        "resource(data://report-template)與分析 prompts(analyze_stock / analyze_stock_full / "
        "compare_stocks / build_stock_report(買方深度 HTML 報告:四面向+期權+法說會+籌碼,圖表必備)/ "
        "company_profile(介紹型 HTML 報告:這家公司在幹嘛,業務取自 10-K Item 1 + web)——第一手 SEC "
        "為招牌、報告語言隨使用者);引用數字或做分析前可先讀。\n"
        "不熟悉本服務 / 第一次使用,先呼叫 start_here 工具拿產品範圍、選工具指引與完整 "
        "workflow 地圖(它也會點出只看 tools/list 容易漏掉的 prompts / resources)。"
    ),
    auth=_build_auth(),
)

# 註冊 tools / resources / prompts(side effect:各模組內的 @mcp.* 裝飾器跑過會把元件
# 掛到 mcp 物件上)。放 server 模組底端避免循環 import。
from trading_agent_mcp import prompts, resources, tools  # noqa: E402, F401
