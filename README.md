# Trading_Agent_MCP

MCP server 對 LLM 暴露 [investor-db](https://github.com/AAAZZZR/Trading_Agent) 的資料工具 —— 美股公司、SEC filings、財務報表、股價、內部人交易、機構持股,以及一個 readonly SQL 自由查詢 tool。

## 架構

```
Claude.ai / Desktop / 其他 MCP client
       │ Streamable HTTP(or stdio)
       │ Authorization: Bearer <MCP_BEARER_TOKEN>
       ▼
trading_agent_mcp  ◄─── 15 個 tool,一對一對應 API endpoint
       │ httpx + Authorization: Bearer <MCP_API_AUTH_TOKEN>
       ▼
Trading_Agent API(FastAPI)
       │
       ▼
PostgreSQL(via SQLAlchemy 2.0 async)
```

跟 `Trading_Agent` 是分開的 GitHub repo,但在 Zeabur 部署時可以放同一個 project / 同一台 server,透過 `<service>.zeabur.internal` 私網互通。可遷移性:所有平台依賴透過環境變數注入,搬 AWS / Cloud Run 只改變數值。

## Tools(16 個)

15 個 per-domain tool 對齊 Trading_Agent 的 `lib/api.ts`,再加 1 個自由 SQL tool:

| Tool | 對應 API endpoint |
|---|---|
| `list_companies` | `GET /api/companies` |
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
| `list_daily_prices` | `GET /api/prices?ticker=...` |
| `get_latest_price` | `GET /api/prices/latest?ticker=...` |
| `list_institutional_holders` | `GET /api/holdings/institutions?ticker=...` |
| `get_holders_breakdown` | `GET /api/holdings/major?ticker=...` |
| `execute_readonly_sql` | 直連 Postgres(`investor_db_readonly` role) |

### `execute_readonly_sql` —— 自由 SELECT

給 power user / AI agent 跑任意 SELECT(含 WITH ... SELECT)。Schema 14 表:
`companies`, `institutions`, `institution_filings`, `filings`, `filing_sections`,
`income_statements`, `balance_sheets`, `cash_flow_statements`, `insider_trades`,
`institutional_holdings`, `prices_hourly`, `financials_quarantine`, `ingest_runs`,
`alembic_version`。

**安全保證(三層防護)**:

1. **SQL parsing**(`sqlparse`):只允許單一 SELECT / WITH ... SELECT,拒絕
   INSERT / UPDATE / DELETE / DROP / CREATE / ALTER / TRUNCATE / GRANT,以及
   `pg_sleep` / `pg_read_server_files` / `lo_import` / `dblink` / `COPY` 等敏感操作。
   Multi-statement(`SELECT 1; DELETE FROM x`)也拒絕。
2. **DB role**:連線使用 `investor_db_readonly` role,GRANT 只有 SELECT。即使第 1 層漏網,
   DB 也擋下任何寫操作。
3. **Statement timeout**:每段查詢 5 秒上限,複雜 query 自動 abort。

**LIMIT 自動處理**:沒寫 → 加 `LIMIT 1000`;寫了 > 10000 → clamp 成 10000。
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

留空 = 不啟用 SQL tool;其他 15 個 tool 不受影響(tool 被呼叫時會回 friendly error)。

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
- [ ] 強化 tool return type(用 pydantic model 取代 dict,LLM 端 schema 更豐富)
- [ ] 加 `list_companies` 的分頁 / 篩選(目前一次回全美股 5000+ 筆)
