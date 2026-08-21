"""`sql_query.py` 純函數測試 —— validation、limit clamp、output truncation。"""

import pytest

from trading_agent_mcp.sql_query import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MAX_OUTPUT_BYTES,
    SQLValidationError,
    clamp_limit,
    truncate_payload,
    validate_select_only,
)

# ---- validate_select_only:接受 SELECT / WITH ---------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select * from companies",
        "SELECT ticker, name FROM companies WHERE sector = 'Technology'",
        "WITH t AS (SELECT 1) SELECT * FROM t",
        "  SELECT 1  ",
        "SELECT 1;",  # 末尾單一分號 OK
    ],
)
def test_validate_accepts_select(sql: str) -> None:
    validate_select_only(sql)  # 不 raise = OK


# ---- validate_select_only:防護不可做過頭(合法用法護欄)---------------------
#
# 上面那組遞迴掃描把「任何一層的寫入語意」都擋掉,很容易順手誤殺正常 SELECT。
# 這組是反向護欄:真實 agent 會下的複雜但唯讀的查詢,必須全部照過。
# 特別注意最後兩筆 —— 字串字面值裡的 DELETE / INTO 不可被當成關鍵字誤殺。


@pytest.mark.parametrize(
    "sql",
    [
        # 普通 SELECT
        "SELECT ticker, name FROM companies WHERE sector = 'Technology'",
        # WITH RECURSIVE CTE
        "WITH RECURSIVE t(n) AS ("
        "SELECT 1 UNION ALL SELECT n + 1 FROM t WHERE n < 10"
        ") SELECT * FROM t",
        # 多層 CTE 全是 SELECT
        "WITH a AS (SELECT 1 AS x), b AS (SELECT 2 AS y) SELECT * FROM a JOIN b ON true",
        # 子查詢
        "SELECT * FROM companies WHERE ticker IN (SELECT ticker FROM prices_daily)",
        # UNION ALL
        "SELECT 1 UNION ALL SELECT 2",
        # 括號包起來的 UNION
        "(SELECT 1) UNION (SELECT 2)",
        # window function
        "SELECT ticker, row_number() OVER (PARTITION BY sector ORDER BY name) AS rn "
        "FROM companies",
        # FETCH FIRST n ROWS ONLY(所以 FETCH 不可進 keyword 黑名單)
        "SELECT * FROM companies ORDER BY ticker FETCH FIRST 10 ROWS ONLY",
        # VALUES 當衍生表
        "SELECT * FROM (VALUES (1),(2)) v(x)",
        # ORDER BY / OFFSET / LIMIT
        "SELECT * FROM companies ORDER BY ticker OFFSET 5 LIMIT 10",
        # 以區塊註解開頭
        "/* pick tech names */ SELECT ticker FROM companies",
        # 字串字面值裡的關鍵字不可誤殺
        "SELECT 'delete me' AS note",
        "SELECT * FROM companies WHERE ticker = 'INTO'",
        # 真實會用到的欄位名(open/high/low/close/adj_close 都不是 sqlparse 關鍵字)
        "SELECT open, high, low, close, volume, adj_close FROM prices_daily "
        "WHERE ticker = 'AAPL'",
    ],
)
def test_validate_accepts_legitimate_complex_select(sql: str) -> None:
    """防護不可誤殺正常唯讀查詢 —— 這組全綠才算沒把 SQL tool 做廢。"""
    validate_select_only(sql)  # 不 raise = OK


# ---- validate_select_only:拒絕 destructive --------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM companies",
        "INSERT INTO companies VALUES ('X', 'Y')",
        "UPDATE companies SET name='X'",
        "DROP TABLE companies",
        "CREATE TABLE foo (id int)",
        "ALTER TABLE companies ADD COLUMN x int",
        "TRUNCATE companies",
        "GRANT SELECT ON companies TO public",
    ],
)
def test_validate_rejects_destructive(sql: str) -> None:
    with pytest.raises(SQLValidationError):
        validate_select_only(sql)


# ---- validate_select_only:拒絕 multi-statement ----------------------------


def test_validate_rejects_multi_statement() -> None:
    with pytest.raises(SQLValidationError, match="Multiple statements"):
        validate_select_only("SELECT 1; DELETE FROM companies")


def test_validate_rejects_multi_select() -> None:
    # 即使兩個都是 SELECT 也擋掉(避免 LLM 拼接被注入第二段)
    with pytest.raises(SQLValidationError, match="Multiple statements"):
        validate_select_only("SELECT 1; SELECT 2")


# ---- validate_select_only:拒絕 empty / whitespace -------------------------


@pytest.mark.parametrize("sql", ["", "   ", "\n\n", ";"])
def test_validate_rejects_empty(sql: str) -> None:
    with pytest.raises(SQLValidationError):
        validate_select_only(sql)


# ---- validate_select_only:拒絕敏感 function ------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_sleep(10)",
        "SELECT pg_read_server_files('/etc/passwd')",
        "SELECT pg_read_binary_file('/x')",
        "SELECT pg_ls_dir('/')",
        "SELECT pg_stat_file('/x')",
        "SELECT lo_import('/etc/passwd')",
        "SELECT lo_export(1, '/tmp/x')",
        "SELECT dblink('host=x', 'select 1')",
        "COPY companies TO '/tmp/x'",
    ],
)
def test_validate_rejects_sensitive(sql: str) -> None:
    with pytest.raises(SQLValidationError):
        validate_select_only(sql)


# ---- validate_select_only:繞過案例(舊版「只看開頭關鍵字」全都放行)-----------
#
# 舊實作只檢查「第一個有意義的 keyword 是不是 SELECT / WITH」+ 對整串做小寫 substring
# 黑名單,於是 `WITH t AS (DELETE FROM companies RETURNING *) SELECT * FROM t` 直接通過
# ——「第一層防護」其實是假的,全靠 readonly DB role 兜底。修正後改成遞迴走訪整棵
# token 樹,任何一層出現寫入語意就拒。以下每一條在舊版都是綠的(= 放行),現在必須全紅。


@pytest.mark.parametrize(
    "sql",
    [
        # --- CTE 內藏 DML(舊版最致命的破口)---
        "WITH t AS (DELETE FROM companies RETURNING *) SELECT * FROM t",
        "WITH t AS (UPDATE companies SET ticker='X' RETURNING *) SELECT count(*) FROM t",
        "WITH t AS (INSERT INTO companies (ticker) VALUES ('X') RETURNING *) SELECT * FROM t",
        "WITH a AS (SELECT 1), b AS (DELETE FROM companies RETURNING *) SELECT * FROM a, b",
        # --- CTE 之後接 DML(data-modifying CTE 的另一種寫法)---
        "WITH t AS (SELECT 1) INSERT INTO companies SELECT * FROM t",
        "WITH t AS (SELECT 1) UPDATE companies SET ticker='X'",
        "WITH t AS (SELECT 1) DELETE FROM companies",
        # --- SELECT ... INTO 會建表寫資料 ---
        "SELECT 1 INTO evil_table",
        "SELECT * INTO evil_table FROM companies",
        # --- 多 statement 拼接 ---
        "SELECT * FROM companies; DROP TABLE companies",
        "SELECT * FROM companies; DELETE FROM companies",
        # --- COPY(含把 SELECT 包進括號的變形)---
        "COPY companies TO '/tmp/x'",
        "COPY (SELECT * FROM companies) TO '/tmp/x'",
        # --- DDL ---
        "TRUNCATE companies",
        "DROP TABLE companies",
        "CREATE TABLE evil (a int)",
        "ALTER TABLE companies ADD COLUMN evil int",
        # --- DCL ---
        "GRANT ALL ON companies TO PUBLIC",
        "REVOKE ALL ON companies FROM PUBLIC",
        # --- 程序 / 動態執行 ---
        "DO $$ BEGIN PERFORM 1; END $$",
        "CALL some_proc()",
        "EXECUTE stmt",
        # --- 會炸 DB / 讀檔 / 有副作用的 function ---
        "SELECT pg_sleep(10)",
        "SELECT pg_sleep_for('10 seconds')",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT lo_import('/etc/passwd')",
        # query_to_xml 會把字串當 SQL 執行 —— parser 看不到那個 DELETE,只能靠函數黑名單
        "SELECT query_to_xml('DELETE FROM companies', true, true, '')",
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity",
        "SELECT nextval('some_seq')",
        "SELECT * FROM dblink('...', 'DELETE FROM companies') AS t(x int)",
        # --- 維護指令 ---
        "VACUUM companies",
    ],
)
def test_validate_rejects_known_bypasses(sql: str) -> None:
    """已知繞過手法必須全部擋在第一層(不是靠 readonly role 兜底)。"""
    with pytest.raises(SQLValidationError):
        validate_select_only(sql)


def test_validate_rejects_when_no_select_present() -> None:
    """通過開頭關鍵字檢查、但整串沒有任何 SELECT → 一樣拒(規則 4 的兜底)。"""
    with pytest.raises(SQLValidationError, match="No SELECT"):
        validate_select_only("WITH t AS (VALUES (1,2)) TABLE t")


# ---- clamp_limit:沒寫 → 外層補 default -----------------------------------


def test_clamp_adds_default_when_missing() -> None:
    out = clamp_limit("SELECT * FROM companies")
    assert f"LIMIT {DEFAULT_LIMIT}" in out


def test_clamp_keeps_semicolon_query_valid() -> None:
    out = clamp_limit("SELECT * FROM companies;")
    assert f"LIMIT {DEFAULT_LIMIT}" in out
    assert ";" not in out


# ---- clamp_limit:過大 → clamp 成外層 MAX ----------------------------------


def test_clamp_clamps_when_too_big() -> None:
    out = clamp_limit("SELECT * FROM companies LIMIT 99999")
    assert out.rstrip().endswith(f"LIMIT {MAX_LIMIT}")


def test_clamp_case_insensitive() -> None:
    out = clamp_limit("SELECT * FROM companies limit 99999")
    assert out.rstrip().endswith(f"LIMIT {MAX_LIMIT}")


# ---- clamp_limit:合理頂層 LIMIT → 外層沿用該值 ---------------------------


@pytest.mark.parametrize("n", [1, 50, 1000, MAX_LIMIT])
def test_clamp_respects_reasonable_top_limit(n: int) -> None:
    out = clamp_limit(f"SELECT * FROM companies LIMIT {n}")
    assert out.rstrip().endswith(f"LIMIT {n}")


# ---- clamp_limit:內層 LIMIT 不可繞過外層上界(本次修復回歸)--------------


def test_clamp_inner_limit_does_not_bypass() -> None:
    sql = "SELECT * FROM insider_trades WHERE ticker IN (SELECT ticker FROM companies LIMIT 5)"
    out = clamp_limit(sql)
    assert "LIMIT 5" in out  # 內層原樣保留
    assert out.rstrip().endswith(f"LIMIT {DEFAULT_LIMIT}")  # 外層補 default


def test_clamp_inner_limit_with_top_limit() -> None:
    out = clamp_limit("SELECT * FROM (SELECT 1 LIMIT 3) x LIMIT 88888")
    assert out.rstrip().endswith(f"LIMIT {MAX_LIMIT}")


# ---- clamp_limit:非整數頂層 LIMIT 不可炸(當作無上界,補 default)-------------
#
# sqlparse 把 'LIMIT 2.5' / 'LIMIT 5e3' 也歸類成 Number token,舊版直接 int() 會丟
# 未捕捉的 ValueError 把 tool 打爆。修正後這種當「無可用上界」處理,外層補 default。


@pytest.mark.parametrize("sql", [
    "SELECT * FROM companies LIMIT 2.5",
    "SELECT * FROM companies LIMIT 5e3",
    "SELECT * FROM companies limit 0.99",
])
def test_clamp_non_integer_limit_falls_back_to_default(sql: str) -> None:
    out = clamp_limit(sql)  # 不 raise
    assert out.rstrip().endswith(f"LIMIT {DEFAULT_LIMIT}")


# ---- truncate_payload -----------------------------------------------------


def test_truncate_passes_small_through() -> None:
    text = '{"rows": []}'
    assert truncate_payload(text) == text


def test_truncate_cuts_big() -> None:
    big = "x" * (MAX_OUTPUT_BYTES + 100)
    out = truncate_payload(big)
    assert "truncated" in out
    assert len(out.encode("utf-8")) < MAX_OUTPUT_BYTES + 500  # head + 簡短註記
