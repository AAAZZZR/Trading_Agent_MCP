-- investor-db readonly role —— `execute_readonly_sql` MCP tool 專用。
--
-- 用 DB 層權限做最後一道防線:即使 sqlparse + statement timeout 漏網,
-- 此 role 也擋下 INSERT / UPDATE / DELETE / DROP / CREATE / ALTER / TRUNCATE。
--
-- 用法(以 superuser 身分對 PROD DB 跑一次):
--   psql "<superuser DSN>" -f scripts/grant_readonly.sql
--
-- 跑完之後:
--   1. 把密碼換成自己的(下面 CHANGE_ME)
--   2. MCP server env var 設:
--      MCP_READONLY_DB_DSN=postgresql://investor_db_readonly:<密碼>@<host>:<port>/<db>
--      ↑ asyncpg 用,不要 `+asyncpg` 後綴

-- ---- 1. 建 role(已存在則改密碼) ---------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'investor_db_readonly') THEN
        CREATE ROLE investor_db_readonly LOGIN PASSWORD 'CHANGE_ME';
    ELSE
        ALTER ROLE investor_db_readonly WITH LOGIN PASSWORD 'CHANGE_ME';
    END IF;
END
$$;

-- ---- 2. 不給寫權限(預設不會有,顯式寫出來當文件) ----------------------
REVOKE ALL ON SCHEMA public FROM investor_db_readonly;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM investor_db_readonly;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM investor_db_readonly;

-- ---- 3. 給 schema USAGE + 既有 / 未來 table 的 SELECT -------------------
GRANT USAGE ON SCHEMA public TO investor_db_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO investor_db_readonly;

-- 未來 alembic migration 新增的 table 也自動 SELECT。
-- 注意:default privileges 只對「執行此 ALTER 的 role 後續建的物件」生效。
-- 如果 migration 用別的 role 跑,要把下行 FOR USER 換成那個 role。
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO investor_db_readonly;
