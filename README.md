# Trading_Agent_MCP

MCP server 對 LLM 暴露 [investor-db](https://github.com/AAAZZZR/Trading_Agent) 的資料工具 —— 美股公司、SEC filings、財務報表、股價、內部人交易、機構持股,以及一個 readonly SQL 自由查詢 tool。

## 架構

```
Claude.ai / Desktop / 其他 MCP client
       │ Streamable HTTP(or stdio)
       │ Authorization: Bearer <MCP_BEARER_TOKEN>
       ▼
trading_agent_mcp  ◄─── 42 個 tool,對應 API endpoint(+ 1 個自由 SQL tool)
       │ httpx + Authorization: Bearer <MCP_API_AUTH_TOKEN>
       ▼
Trading_Agent API(FastAPI)
       │
       ▼
PostgreSQL(via SQLAlchemy 2.0 async)
```

跟 `Trading_Agent` 是分開的 GitHub repo,但在 Zeabur 部署時可以放同一個 project / 同一台 server,透過 `<service>.zeabur.internal` 私網互通。可遷移性:所有平台依賴透過環境變數注入,搬 AWS / Cloud Run 只改變數值。

### 認證(三種模式,`_build_auth()` 依 settings 擇一)

| 模式 | 啟用條件 | 行為 |
|---|---|---|
| **Per-user(SaaS)** | `MCP_PER_USER_AUTH=true` 且 `MCP_API_BASE_URL` 有值 | 每個 user 帶自己的 API key;`PerUserTokenVerifier` 對「每次請求」打後端 `POST /api/mcp/authorize`(帶 service token)驗證 + 計量。`ok=true` 回 `AccessToken`(scopes 反映 tier);`ok=false` / HTTP error 回 `None` → 401。 |
| **共用 token** | per-user 關 + `MCP_BEARER_TOKEN` 非空 | `StaticTokenVerifier` 驗單一共用 token(scope = `tier:pro`,完整權限)。向後相容舊部署。 |
| **無 auth** | 兩者皆無 | 不啟用 verifier(stdio 本機開發用)。 |

本 server 不公告 OAuth metadata(verifier 不掛 `.well-known` route),所以 client 拿到 401 會直接顯示認證失敗,不會誤入 OAuth 流程。

**Tier gating**:`execute_readonly_sql` 與 `describe_table`(重量級自由 SQL / schema 自省)用 `require_scopes("tier:pro")` 在 FastMCP 元件層 gating —— free tier(`tier:free`)在 `tools/list` 看不到這兩個 tool,直接呼叫也被擋;其餘 structured tool free / pro 都能用。

**快取取捨**:per-user 模式有一個 ~20s 的 in-process 快取,但「只」存 validity / tier,做為 authorize 短暫失敗(網路抖動 / 5xx)時的 fallback,不取代每次 POST —— 計量與配額永遠以後端每次呼叫為準。

## Tools(42 個)

41 個 per-domain tool(對齊 Trading_Agent 的 `lib/api.ts` 與聚合 endpoint),再加 1 個自由 SQL tool:

| Tool | 對應 API endpoint |
|---|---|
| `list_companies` | `GET /api/companies` |
| `search_companies` | `GET /api/companies/search?q=...` |
| `get_company` | `GET /api/companies/{ticker}` |
| `list_filings` | `GET /api/filings?ticker=...` |
| `get_filing` | `GET /api/filings/{accession}` |
| `list_filing_sections` | `GET /api/filings/{accession}/sections` |
| `get_filing_section` | `GET /api/filings/{accession}/sections/{item_code}` |
| `get_income_statements` | `GET /api/financials/income?ticker=...` |
| `get_balance_sheets` | `GET /api/financials/balance?ticker=...` |
| `get_cash_flow_statements` | `GET /api/financials/cashflow?ticker=...` |
| `get_latest_period` | `GET /api/financials/latest?ticker=...` |
| `list_insider_trades` | `GET /api/insider?ticker=...` |
| `list_daily_prices` | `GET /api/prices/daily?ticker=...`(第一手日 K,~5 年) |
| `get_latest_price` | `GET /api/prices/daily/latest?ticker=...` |
| `list_hourly_prices` | `GET /api/prices/hourly?ticker=...` |
| `list_institutional_holders` | `GET /api/holdings/institutions?ticker=...` |
| `get_holders_breakdown` | `GET /api/holdings/major?ticker=...` |
| `list_13f_holders` | `GET /api/13f/holders?ticker=...` |
| `list_13f_portfolio` | `GET /api/13f/portfolio?cik=...` |
| `list_13f_top_buyers` | `GET /api/13f/top-buyers?ticker=...` |
| `list_13f_top_sellers` | `GET /api/13f/top-sellers?ticker=...` |
| `search_institutions` | `GET /api/13f/institutions/search?q=...` |
| `get_institution` | `GET /api/13f/institutions/{cik}` |
| `list_dividends` | `GET /api/dividends/{ticker}` |
| `list_splits` | `GET /api/splits/{ticker}` |
| `list_earnings` | `GET /api/earnings/{ticker}` |
| `get_earnings_calendar` | `GET /api/earnings/calendar` |
| `get_etf_profile` | `GET /api/etf/{ticker}/profile` |
| `list_etf_holdings` | `GET /api/etf/{ticker}/holdings` |
| `list_etf_sectors` | `GET /api/etf/{ticker}/sectors` |
| `list_etfs_holding_ticker` | `GET /api/etf/holders/{ticker}` |
| `list_macro_series` | `GET /api/macro/series` |
| `get_macro_series` | `GET /api/macro/series/{series_id}` |
| `get_overview` | `GET /api/overview/{ticker}` |
| `get_options_chain` | `GET /api/options/chain?underlying=...` |
| `get_option_expirations` | `GET /api/options/expirations?underlying=...` |
| `get_option_contract_history` | `GET /api/options/contract/{contract_id}` |
| `get_analysis` | `GET /api/analysis/{ticker}`(四面向紅綠燈,帶解讀) |
| `get_objective_report` | `GET /api/report/{ticker}`(客觀數據包,純數據) |
| `screen_insider_buys` | `GET /api/screener/insider-buys` |
| `execute_readonly_sql` | 直連 Postgres(`investor_db_readonly` role) |
| `describe_table` | 直連 Postgres(`information_schema` 自省) |

### `execute_readonly_sql` —— 自由 SELECT

給 power user / AI agent 跑任意 SELECT(含 WITH ... SELECT)。可查的表:
`companies`, `institutions`, `institution_filings`, `filings`, `filing_sections`,
`income_statements`, `balance_sheets`, `cash_flow_statements`, `insider_trades`,
`institutional_holdings`, `prices_daily`, `prices_hourly`, `company_overview`,
`options_eod`, `financials_quarantine`, `ingest_runs`, `alembic_version`,
`dividends`, `splits`, `earnings_calendar`, `etf_profile`, `etf_holdings`,
`macro_series`, `macro_series_meta`。實際清單以 `describe_table()`(不帶參數)為準。

**安全保證(三層防護)**:

1. **SQL parsing**(`sqlparse`):只允許單一 SELECT / WITH ... SELECT,拒絕
   INSERT / UPDATE / DELETE / DROP / CREATE / ALTER / TRUNCATE / GRANT,以及
   `pg_sleep` / `pg_read_server_files` / `lo_import` / `dblink` / `COPY` 等敏感操作。
   Multi-statement(`SELECT 1; DELETE FROM x`)也拒絕。
2. **DB role**:連線使用 `investor_db_readonly` role,GRANT 只有 SELECT。即使第 1 層漏網,
   DB 也擋下任何寫操作。
3. **Statement timeout**:每段查詢 5 秒上限,複雜 query 自動 abort。

**LIMIT 自動處理**:把查詢包成 `SELECT * FROM (<你的 SQL>) _ LIMIT n` 加硬性外層上界 —— 沒寫頂層 LIMIT 補 `LIMIT 1000`,有頂層 LIMIT N 用 `min(N, 10000)`;子查詢內寫 LIMIT 也無法繞過。
**Output 截斷**:序列化超過 100KB 截斷並附註記。

#### DB role 設置(用戶手動跑一次)

```sh
# 1. 編輯 scripts/grant_readonly.sql 把 CHANGE_ME 換成自己的密碼
# 2. 用 superuser 跑(對 PROD DB):
psql "<superuser DSN>" -f scripts/grant_readonly.sql
```

#### 環境變數

`execute_readonly_sql` 額外要設一個 env var:

```
MCP_READONLY_DB_DSN=postgresql://investor_db_readonly:<密碼>@<host>:<port>/<db>
```

注意是 `postgresql://`(asyncpg 用,不要 `+asyncpg` 後綴)。

留空 = 不啟用 SQL tool(`execute_readonly_sql` / `describe_table`);其餘 per-domain tool 不受影響(這兩個 tool 被呼叫時會回 friendly error)。

## 本機開發

```sh
cp .env.example .env
# 編輯 .env:填 MCP_API_BASE_URL(本機 http://localhost:8000)、
# MCP_API_AUTH_TOKEN(對 Trading_Agent API 的 bearer)、
# MCP_BEARER_TOKEN(client 連本 server 用的 bearer)

uv sync

# Streamable HTTP mode(對外服務,預設 port 8000)
uv run python -m trading_agent_mcp

# 或 stdio mode(給 Claude Desktop 本機 spawn)
uv run python -m trading_agent_mcp --stdio
```

## 部署(Zeabur)

1. 把本 repo 加進 `investor-db` Zeabur project,類型 web service。
2. Root directory 留空(Zeabur 自動偵測 root `Dockerfile`)。
3. Variables 設:`MCP_API_BASE_URL=http://api.zeabur.internal:8080`、`MCP_API_AUTH_TOKEN=...`、`MCP_BEARER_TOKEN=...`。
4. 開對外 domain(client 要連)—— Zeabur 自動 HTTPS。

## 測試

```sh
uv run pytest -q
```

## TODO

- [x] Bearer auth(StaticTokenVerifier)—— 完成於 commit `0f86383`,server.py `_build_auth()`
- [x] Per-user API-key auth(PerUserTokenVerifier)+ tier gating —— `auth.py`,打後端 `/api/mcp/authorize`
- [ ] 強化 tool return type(用 pydantic model 取代 dict,LLM 端 schema 更豐富)
- [ ] 加 `list_companies` 的分頁 / 篩選(目前一次回全美股 5000+ 筆)
