"""FastMCP server 設定 —— 註冊 server identity、tools 與 middleware。

Tools 在 `trading_agent_mcp.tools` 透過 `@mcp.tool` 註冊;import 即生效。

Auth(`_build_auth` 決定,優先序由上到下):
  1. OAuth 2.1 / Google 上游(google client id + secret + public base url + saas DSN
     四者齊全)→ StockfactsGoogleProvider:client 自動發現 metadata、彈瀏覽器登入、
       自動拿 access + refresh token。`idb_` 開頭的自家 API key 仍由它轉交給
       PerUserTokenVerifier,所以兩條路並存。
  2. per-user 模式(mcp_per_user_auth=True 且 mcp_saas_database_url 有值)
     → PerUserTokenVerifier:每個 user 帶自己的 API key,直接對 SaaS 控制面 DB
       驗證(帶正向快取);計量另由 UsageMiddleware 在 tool 層非同步寫。
  3. 單一共用 token(mcp_bearer_token 非空)→ StaticTokenVerifier(舊行為 / 本機)。
  4. per-user 開著但兩個憑據來源都沒有 → MisconfiguredVerifier(一律 401,不 fail open)。
  5. per-user 明確關閉且無共用 token → None(不啟用 auth;stdio 本機開發 / dev container)。

OAuth 排最前面是因為它是主線(使用者完全不必手動貼 key);設定不全時
`build_oauth_provider` 回 None,靜靜落到第 2 條,不會讓 server 起不來。
"""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from trading_agent_mcp.auth import MisconfiguredVerifier, PerUserTokenVerifier
from trading_agent_mcp.oauth import StockfactsGoogleProvider, build_oauth_provider
from trading_agent_mcp.settings import settings
from trading_agent_mcp.usage import UsageMiddleware


def _build_auth() -> AuthProvider | None:
    """依 settings 決定 auth provider;見模組 docstring 的優先序。"""
    # 1. OAuth(主線)—— 四個設定齊全才成立,缺任何一個回 None 落到下一條。
    oauth = build_oauth_provider(
        client_id=settings.mcp_google_client_id,
        client_secret=settings.mcp_google_client_secret,
        public_base_url=settings.mcp_public_base_url,
        saas_database_url=settings.mcp_saas_database_url,
        cache_ttl=settings.mcp_auth_cache_ttl,
        stale_ttl=settings.mcp_auth_stale_ttl,
        quota_ttl=settings.mcp_quota_cache_ttl,
    )
    if oauth is not None:
        return oauth

    # 2. Per-user SaaS 模式 —— 需要 SaaS 控制面 DSN 才驗得了 key,否則退回 static。
    if settings.mcp_per_user_auth and settings.mcp_saas_database_url:
        return PerUserTokenVerifier(
            cache_ttl=settings.mcp_auth_cache_ttl,
            stale_ttl=settings.mcp_auth_stale_ttl,
            quota_ttl=settings.mcp_quota_cache_ttl,
        )

    # 3. 單一共用 token(向後相容:本機 / 舊部署)。
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

    # 4. per-user 開著(這是預設值)卻沒有 DSN、也沒有共用 token —— 設定失誤。
    #    這裡**不能**掉到下面的「不啟用 auth」:mcp_per_user_auth 為真代表這台是
    #    要對外服務的,少設一個環境變數就把整台 server 敞開,是最糟的失敗模式。
    #    改成一律 401(見 MisconfiguredVerifier 的 docstring)。
    if settings.mcp_per_user_auth:
        return MisconfiguredVerifier()

    # 5. 明確關掉 per-user 又沒有共用 token —— 本機 stdio / dev container,不啟用 auth。
    return None


# 先算出 verifier 再傳進 FastMCP —— 下面要靠它判斷該不該掛計量 middleware。
_auth = _build_auth()

mcp = FastMCP(
    # 對外的 server 身分。這個字串會出現在兩個使用者看得到的地方:MCP client 的
    # server 清單,以及 OAuth 同意頁的標題與字標(`ConsentMixin` 直接讀 FastMCP.name)。
    # 舊值 `investor-db` 是內部代號,不該印在使用者臉上。
    # ⚠️ 這不影響工具名稱前綴 —— Claude Code / Desktop 用的是使用者自己在設定檔裡
    # 取的鍵名(`mcp__<設定鍵>__<tool>`),不是這裡的值。
    name="Livermore",
    # 同意頁會把字標連到這裡;順帶讓 client 有個「這是誰」的去處。
    website_url="https://livermore.club",
    instructions=(
        "美股市場資料查詢 —— 公司基本資料與搜尋、SEC filings(10-K / 10-Q / 8-K / Form 4)、"
        "財務報表(損益 / 資產負債 / 現金流)、估值快照(市值 / 本益比家族 / 均線)、"
        "第一手日 K(~5 年)/ 每小時股價、內部人交易與跨市場篩選、"
        "機構持股(持股比例概覽 + 第一手 SEC 13F + filer 反查)、公司行動(現金股息 / 股票分割)、"
        "財報行事曆(單一公司 + 跨市場區間掃描)、法說會逐字稿(管理層語氣 / 每段情緒)、"
        "新聞 + 情緒(vendor 聚合,單一公司 + 全市場)、"
        "FINRA 未平倉空單(每月兩次)與場外每日 short-sale volume(兩者不可混用)、"
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
    auth=_auth,
)

# 計量掛在「claims 帶得出 user_id」的兩種模式(OAuth / per-user bearer)。
# 其他模式的 client_id 是共用身分(不是 user uuid),硬記會每次 INSERT 都撞
# 型別 / 外鍵而失敗,只會刷 log。
if isinstance(_auth, StockfactsGoogleProvider | PerUserTokenVerifier):
    mcp.add_middleware(UsageMiddleware())


def _should_rewrite_401(auth: AuthProvider | None) -> bool:
    """401 文案要不要改寫 —— 由實際的 auth 模式決定。

    OAuth 模式下 401 是「請去認證」的正常訊號,框架原本那句「clear tokens and
    reconnect」描述的正是 client 該做的事,原樣放行才對;bearer-only 模式沒有
    OAuth 可走,那句話只會讓使用者刪掉手上好好的 key,要換掉。
    詳見 `auth.AuthErrorMessageMiddleware` 的 docstring。
    """
    return not isinstance(auth, StockfactsGoogleProvider)


# `__main__.py` 建 ASGI middleware 時用。
rewrite_401_description = _should_rewrite_401(_auth)

# 註冊 tools / resources / prompts(side effect:各模組內的 @mcp.* 裝飾器跑過會把元件
# 掛到 mcp 物件上)。放 server 模組底端避免循環 import。
from trading_agent_mcp import prompts, resources, tools  # noqa: E402, F401
