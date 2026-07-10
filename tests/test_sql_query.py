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
