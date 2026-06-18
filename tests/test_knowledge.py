"""resources.py / prompts.py 的單元測試 —— 知識層有註冊 + 內容含關鍵錨點。

策略(對齊 test_tools.py 的註冊測試):import side-effect 後,
  1. 兩個 resource URI(data://dictionary、data://analysis-playbook)有掛上去。
  2. 三個 prompt 名(analyze_stock、analyze_stock_full、compare_stocks)有掛上去。
  3. resource / prompt 渲染出的文字含幾個 load-bearing 錨點(TTM、45-day、not-covered…),
     確保領域知識真的寫進去而不是空殼。

FastMCP 3.x 的 async 內省 API:list_resources() / list_prompts() / read_resource(uri) /
render_prompt(name, args)。
"""

# import 觸發 server / resources / prompts 的註冊 side-effect。
from trading_agent_mcp import prompts, resources  # noqa: F401
from trading_agent_mcp.server import mcp

# ---- 註冊性測試 -----------------------------------------------------------


async def test_resources_registered() -> None:
    """兩個 markdown resource 的 URI 都註冊到 mcp。"""
    registered = await mcp.list_resources()
    uris = {str(r.uri) for r in registered}
    expected = {"data://dictionary", "data://analysis-playbook"}
    assert expected <= uris, f"missing resources: {expected - uris}"


async def test_prompts_registered() -> None:
    """三個 prompt 的名字都註冊到 mcp。"""
    registered = await mcp.list_prompts()
    names = {p.name for p in registered}
    expected = {"analyze_stock", "analyze_stock_full", "compare_stocks"}
    assert expected <= names, f"missing prompts: {expected - names}"


# ---- 內容錨點測試 ---------------------------------------------------------


async def _read_resource_text(uri: str) -> str:
    result = await mcp.read_resource(uri)
    return "".join(c.content for c in result.contents)


async def _render_prompt_text(name: str, args: dict) -> str:
    result = await mcp.render_prompt(name, args)
    return "\n".join(m.content.text for m in result.messages)


async def test_dictionary_content_anchors() -> None:
    """資料字典含關鍵語意錨點(單位 / 調整 / 代碼 / 滯後 / null 規則)。"""
    text = await _read_resource_text("data://dictionary")
    for anchor in (
        "TTM",  # 財務:TTM 加總規則
        "45-day",  # 13F 法定申報滯後
        "not-covered",  # market_cap 缺 = 未覆蓋 ≠ 0
        "fiscal_period",  # Q1-Q4 / FY 值域
        "transaction_code",  # 內部人代碼對照
        "change_type",  # 13F 增減值域
        "adj_close",  # prices 調整語意
        "0-1 decimal",  # 比率非百分比
        "case-sensitive",  # macro series_id
        "eps_diluted",  # 財務關鍵欄位
        "market_movers",  # 全市場漲跌榜 domain
        "ipo_calendar",  # IPO 行事曆 domain
        "delisted_at",  # 下市偵測語意(status + 日期)
        "not-yet-priced",  # IPO 未定價 = null ≠ $0
        "earnings_call_transcripts",  # 逐字稿 domain
        "Tombstone semantics",  # 逐字稿 tombstone(segments=0)語意
        "news_ticker_sentiment",  # 新聞情緒 domain
        "VENDOR AGGREGATION",  # 新聞為 vendor 聚合源(非第一手)
        "min_relevance >= 0.5",  # 新聞 relevance 訊號門檻
    ):
        assert anchor in text, f"data dictionary missing anchor: {anchor!r}"


async def test_playbook_resource_content_anchors() -> None:
    """analysis-playbook resource(prompts 文件版)含 step 化方法論與誠實規則。"""
    text = await _read_resource_text("data://analysis-playbook")
    for anchor in ("Step 0", "Step 5", "TTM", "analyst_target_price"):
        assert anchor in text, f"analysis-playbook missing anchor: {anchor!r}"
    # cluster buying:句首大寫 / 內文小寫皆可,語意錨點不該因大小寫脆弱。
    assert "cluster buying" in text.lower(), "analysis-playbook missing 'cluster buying'"


async def test_analyze_stock_prompt_content() -> None:
    """analyze_stock:第一行要求用使用者語言 + 帶 ticker + step 化指令。"""
    text = await _render_prompt_text("analyze_stock", {"ticker": "AAPL"})
    assert text.splitlines()[0].startswith("Respond in the user's language")
    assert "AAPL" in text
    for anchor in ("Step 0", "list_daily_prices", "TTM", "analyst_target_price"):
        assert anchor in text, f"analyze_stock missing anchor: {anchor!r}"


async def test_analyze_stock_full_prompt_content() -> None:
    """analyze_stock_full:四面向 + 紅綠燈 + 缺資料規則 + 13F 滯後 + IV。"""
    text = await _render_prompt_text("analyze_stock_full", {"ticker": "NVDA"})
    assert text.splitlines()[0].startswith("Respond in the user's language")
    assert "NVDA" in text
    for anchor in (
        "N/4",  # 缺資料面向從分母剔除、基於 N/4
        "45-day",  # 13F 滯後
        "implied_volatility",  # 期權面 IV
        "execute_readonly_sql",  # pro tier 聚合
    ):
        assert anchor in text, f"analyze_stock_full missing anchor: {anchor!r}"
    # cluster buying:句首大寫,case-insensitive 比對。
    assert "cluster buying" in text.lower(), "analyze_stock_full missing 'cluster buying'"


async def test_compare_stocks_prompt_content() -> None:
    """compare_stocks:兩 ticker 都帶入 + 對照表 + 共同風險。"""
    text = await _render_prompt_text("compare_stocks", {"ticker_a": "KO", "ticker_b": "PEP"})
    assert text.splitlines()[0].startswith("Respond in the user's language")
    assert "KO" in text and "PEP" in text
    for anchor in ("comparison table", "shared risk", "YoY"):
        assert anchor in text, f"compare_stocks missing anchor: {anchor!r}"


# ---- build_stock_report prompt + report-template resource (HTML report skill) ----


async def test_report_template_resource_registered() -> None:
    """HTML 報告模板 resource 有註冊。"""
    registered = await mcp.list_resources()
    uris = {str(r.uri) for r in registered}
    assert "data://report-template" in uris, "missing data://report-template resource"


async def test_build_stock_report_prompt_registered() -> None:
    """build_stock_report prompt 有註冊。"""
    registered = await mcp.list_prompts()
    names = {p.name for p in registered}
    assert "build_stock_report" in names, "missing build_stock_report prompt"


async def test_report_template_content_anchors() -> None:
    """模板含 self-contained HTML 骨架、雙字體、紅綠燈、列印與免責錨點。"""
    text = await _read_resource_text("data://report-template")
    for anchor in (
        "<style",  # 內嵌 CSS,自包含單檔
        "Inter",  # 敘述字體
        "JetBrains Mono",  # 數字等寬字體
        "verdict",  # BLUF 裁決卡 class
        "kpi",  # KPI 儀表板 class
        "cmp",  # 對照表 class
        "@media print",  # 列印樣式
        "🟢",  # 紅綠燈(顏色不單獨表意,配 emoji/字)
        "Not investment advice",  # 免責
        "As of",  # as-of 新鮮度戳記
    ):
        assert anchor in text, f"report-template missing anchor: {anchor!r}"


async def test_build_stock_report_prompt_content() -> None:
    """單檔模式:語言行 + ticker + HTML/模板指示 + 第一手招牌面板 + 誠實/新鮮度 + web overlay 雙分支。"""
    text = await _render_prompt_text("build_stock_report", {"ticker": "TSLA"})
    assert text.splitlines()[0].startswith("Respond in")
    assert "TSLA" in text
    for anchor in (
        "data://report-template",  # 讀模板 resource
        "self-contained",  # 單一自包含 HTML
        "N/4",  # 缺面向剔除分母
        "45-day",  # 13F 滯後
        "cluster buying",  # 內部人招牌面板
        "skew",  # 期權部位 / 隱含預期取代臆造目標價
        "as-of",  # 每塊資料標 as-of 新鮮度
        "real-time",  # 非實時定位(deliberate scope)
        "EOD",  # EOD / 申報為準
        "web search",  # web 新聞 overlay(有 web 工具)
        "get_company_news",  # 無 web 工具的退路
        "EDGAR",  # 第一手出處宣告
    ):
        assert anchor in text, f"build_stock_report missing anchor: {anchor!r}"


async def test_build_stock_report_peers_comparison() -> None:
    """peers 非空 → 切換對照版,兩 ticker 都帶入 + 對照表指示。"""
    text = await _render_prompt_text("build_stock_report", {"ticker": "AAOI", "peers": "COHR"})
    assert "AAOI" in text and "COHR" in text
    assert "comparison" in text.lower(), "peer mode should instruct a comparison layout"
