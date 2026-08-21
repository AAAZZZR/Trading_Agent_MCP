"""`execute_readonly_sql` tool 的純函數驗證層。

跟 DB I/O 解耦,方便單元測試。三件事:

1. `validate_select_only(sql)` —— 用 sqlparse 解析後**遞迴走訪整棵 token 樹**,
   任何一層(CTE / 子查詢 / 括號內)出現寫入語意就拒。不是「只看開頭關鍵字」——
   舊版那樣做會被 `WITH t AS (DELETE ... RETURNING *) SELECT * FROM t` 整個繞過。
2. `clamp_limit(sql)` —— 用 sqlparse 取頂層 LIMIT 算上界(無→DEFAULT_LIMIT,
   有 N→min(N, MAX_LIMIT)),再把查詢包成 `SELECT * FROM (<sql>) _ LIMIT 上界`,
   外層硬界一定生效(關死子查詢內 LIMIT 繞過)。
3. `truncate_payload(text)` —— serialize 後超過 MAX_OUTPUT_BYTES 就截斷加註記。

DB role + sqlparse + statement_timeout = 三層防護。本檔只負責第二層。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import sqlparse
from sqlparse.sql import Statement
from sqlparse.tokens import DDL, DML, Keyword, Number

# ---- 常數 -----------------------------------------------------------------

DEFAULT_LIMIT = 1000
MAX_LIMIT = 10000
MAX_OUTPUT_BYTES = 100 * 1024  # 100KB

# 敏感 function 黑名單 —— 即使 readonly role 擋 DML,這些在 SELECT-context 下仍可能
# 炸 DB、讀檔、或**把字串當 SQL 執行**繞過本層 parser(query_to_xml 就是這種)。
# 比對時轉小寫做 substring 檢查(粗暴但夠用)。
# 注意:做 substring match 會誤殺欄位名(例如有人欄位叫 copy_url / nextval_seq),
# 但這層只是第二道防線,真的炸不到 DB(role 擋住);誤殺再放寬。
_BLOCKED_FUNCTIONS = (
    "pg_sleep",  # 也涵蓋 pg_sleep_for / pg_sleep_until(substring 比對)
    "pg_read_file",  # 注意:pg_read_binary_file 不含此子字串,兩個都要列
    "pg_read_server_files",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "lo_import",
    "lo_export",
    "lo_get",
    "lo_put",
    "lo_unlink",
    "dblink",
    "query_to_xml",  # 會把傳入字串當 SQL 執行 —— 完全繞過本檔的 token 掃描
    "pg_terminate_backend",
    "pg_cancel_backend",
    "pg_reload_conf",
    "pg_logical_emit_message",
    "pg_advisory_lock",
    "nextval",  # 序號推進 = 有副作用,不是唯讀
    "setval",
)

# 遞迴 token 掃描用的 keyword 黑名單(比對 `token.normalized.upper()`,精確相等而非
# substring,所以欄位名 copy_url / start_date 這種不會被誤殺 —— 它們是 Name 不是 Keyword)。
# 刻意**不含** FETCH(`FETCH FIRST n ROWS ONLY` 是合法 SELECT 語法)、
# 也不含 SET / ANALYZE(誤殺風險 > 收益,statement 開頭那層已經擋掉 `SET ...`)。
_BLOCKED_KEYWORDS = frozenset(
    {
        "COPY",
        "GRANT",
        "REVOKE",
        "INTO",  # `SELECT ... INTO new_table` 會建表寫資料
        "CALL",
        "DO",
        "EXECUTE",
        "PREPARE",
        "DEALLOCATE",
        "DECLARE",
        "LISTEN",
        "NOTIFY",
        "UNLISTEN",
        "LOCK",
        "VACUUM",
        "REINDEX",
        "CLUSTER",
        "REFRESH",
        "COMMIT",
        "ROLLBACK",
        "SAVEPOINT",
        "BEGIN",
        "START",
        "DISCARD",
        "IMPORT",
    }
)


class SQLValidationError(ValueError):
    """SQL 不通過驗證 —— tool 應該回 friendly error 而非 raise 給 LLM。"""


# ---- 驗證 -----------------------------------------------------------------


def validate_select_only(sql: str) -> None:
    """SQL 必須是單一唯讀 SELECT(可帶 WITH CTE)。不通過 raise SQLValidationError。

    流程:
      1. sqlparse.parse() 切 statements,移除空白後必須 == 1 個。
      2. 該 statement 第一個有意義的 keyword 必須是 SELECT 或 WITH(早期 friendly error)。
      3. **遞迴走訪整棵 token 樹**:任何一層出現 DML(非 SELECT)/ DDL / 黑名單 keyword
         就拒,並且整串至少要有一個 SELECT。這步是真正的防護 —— 只看開頭關鍵字時
         `WITH t AS (DELETE FROM companies RETURNING *) SELECT * FROM t` 會整個放行。
      4. 整串小寫做 function blocklist substring 比對(pg_sleep / query_to_xml 等);
         字串字面值裡的 SQL(query_to_xml 那種)不會被 tokenize 成 DML,只能靠這層。
    """
    if not sql or not sql.strip():
        raise SQLValidationError("SQL is empty.")

    parsed = sqlparse.parse(sql)
    # 過掉純空白 statement(末尾分號會產生一個空 statement)
    statements: list[Statement] = [s for s in parsed if str(s).strip()]

    if len(statements) == 0:
        raise SQLValidationError("SQL is empty.")
    if len(statements) > 1:
        raise SQLValidationError(
            "Multiple statements are not allowed; submit one SELECT at a time."
        )

    stmt = statements[0]
    first_keyword = _first_significant_keyword(stmt)
    if first_keyword is None:
        raise SQLValidationError("Could not parse SQL; expected a SELECT statement.")

    if first_keyword not in ("SELECT", "WITH"):
        raise SQLValidationError(
            f"Only SELECT (or WITH ... SELECT) is allowed; got {first_keyword}."
        )

    _reject_write_semantics(stmt)

    # 黑名單比對:lower-case substring。複雜情境(例如 identifier 帶 "pg_sleep")
    # 會誤殺,但寧錯殺不放過 —— DB role 是真正的最後防線。
    lowered = sql.lower()
    for blocked in _BLOCKED_FUNCTIONS:
        if blocked in lowered:
            raise SQLValidationError(
                f"Use of '{blocked}' is not allowed: this tool only runs read-only "
                "SELECT queries with no side effects."
            )


def _iter_all_tokens(token: Any) -> Iterator[Any]:
    """深度優先走訪 token 與其所有子 token。

    sqlparse 會把 CTE / 子查詢 / 括號包成 group token(Parenthesis / Identifier /
    IdentifierList …),group 本身 `ttype is None`、真正的關鍵字藏在 `.tokens` 裡。
    只掃 statement 最外層 token list 會完全看不到 `WITH t AS (DELETE ...)` 的 DELETE。
    """
    yield token
    for child in getattr(token, "tokens", ()):
        yield from _iter_all_tokens(child)


def _reject_write_semantics(stmt: Statement) -> None:
    """遞迴掃描 statement,任何一層有寫入語意就 raise;並要求至少存在一個 SELECT。

    sqlparse 0.5 的實測分類(這些規則就是照著它訂的):
      - `WITH t AS (DELETE ...)` 的 DELETE  → Token.Keyword.DML
      - `WITH t AS (SELECT 1) INSERT INTO`  → INSERT 是 Keyword.DML、INTO 是 Keyword
      - `SELECT 1 INTO evil_table`          → INTO 是 Keyword
      - `TRUNCATE` / `DROP` / `CREATE` / `ALTER` → Keyword.DDL
      - `COPY ... TO ...`                   → COPY 是 Keyword
      - `GRANT` / `REVOKE`                  → Keyword.DCL(是 Keyword 的 subtype,吃得到)
    """
    has_select = False

    for token in _iter_all_tokens(stmt):
        ttype = token.ttype
        if ttype is None:  # group token,本身沒語意,子 token 會另外走到
            continue
        normalized = token.normalized.upper()

        if ttype in DML:
            if normalized == "SELECT":
                has_select = True
                continue
            raise SQLValidationError(
                f"Only read-only SELECT is allowed; found a '{normalized}' statement. "
                "Writes are rejected even when nested inside a CTE or subquery."
            )
        if ttype in DDL:
            raise SQLValidationError(
                f"Schema-changing statements are not allowed; found '{normalized}'."
            )
        if ttype in Keyword and normalized in _BLOCKED_KEYWORDS:
            raise SQLValidationError(
                f"'{normalized}' is not allowed: this tool only runs read-only SELECT "
                "queries. Rewrite the query without it."
            )

    if not has_select:
        raise SQLValidationError(
            "No SELECT was found; submit a read-only SELECT (optionally with CTEs)."
        )


def _first_significant_keyword(stmt: Statement) -> str | None:
    """回傳 statement 第一個 DML / Keyword token 的大寫值。

    跳過空白、註解;遇到第一個 DML(SELECT / INSERT 等)或 Keyword(WITH / GRANT 等
    包含其 subtype 如 Keyword.CTE / Keyword.DCL)就回。`tok.ttype in Keyword` 是 sqlparse
    token-type hierarchy 比對(類 isinstance),會吃 subtype。
    """
    for token in stmt.tokens:
        if token.is_whitespace:
            continue
        if token.ttype is not None and (token.ttype in DML or token.ttype in Keyword):
            return token.normalized.upper()
        # parenthesized 開頭 (例如 `(SELECT ...)` UNION ...) 也算 SELECT-like
        if hasattr(token, "tokens"):
            inner = _first_significant_keyword(token)
            if inner is not None:
                return inner
    return None


# ---- LIMIT 注入 / 截斷 ----------------------------------------------------


def _top_level_limit(sql: str) -> int | None:
    """回傳 statement「頂層」LIMIT 的整數值;沒有或非整數則 None。

    子查詢 / 衍生表 / CTE 內的 LIMIT 會被 sqlparse 包進 Parenthesis / Function token,
    只走 statement 最外層 token list 自然跳過它們 —— 這正是舊 regex 版(抓「第一個」
    LIMIT)被內層 LIMIT 繞過的根因修正。只認 `LIMIT <整數常數>`;`LIMIT ALL` / `LIMIT $1`
    / 非整數常數(`LIMIT 2.5` / `5e3`,sqlparse 仍歸類成 Number 但 int() 會丟 ValueError)
    一律視同「無可用上界」回 None(交給 wrap 補 default)。
    """
    statements = [s for s in sqlparse.parse(sql) if str(s).strip()]
    if not statements:
        return None
    tokens = [t for t in statements[0].tokens if not t.is_whitespace]
    for i, token in enumerate(tokens):
        if token.ttype is Keyword and token.normalized.upper() == "LIMIT":
            for nxt in tokens[i + 1:]:
                if nxt.ttype in Number:
                    try:
                        return int(nxt.value)
                    except ValueError:
                        # 合法 Postgres 但非整數的 LIMIT(如 2.5 / 5e3)—— 不當明確上界,
                        # 回 None 讓外層補 default。真的非法 LIMIT 由 DB 端報錯,tool 不炸。
                        return None
                return None  # LIMIT 後第一個有意義 token 非數字常數 → 無上界
    return None


def _strip_trailing_semicolons(sql: str) -> str:
    """去掉結尾分號 + 空白 —— 包成子查詢時 `(... ;)` 會 parse error。"""
    stripped = sql.strip()
    while stripped.endswith(";"):
        stripped = stripped[:-1].rstrip()
    return stripped


def clamp_limit(sql: str, default: int = DEFAULT_LIMIT, maximum: int = MAX_LIMIT) -> str:
    """確保 SQL 帶硬性外層 LIMIT 上界,回傳改寫後 SQL。

    做法:用 sqlparse 取頂層 LIMIT 算出上界 bound,再把整段查詢包進子查詢加外層 LIMIT:
    `SELECT * FROM (<user_sql>) AS _capped LIMIT bound`。
      - 無頂層 LIMIT → bound = default。
      - 有頂層 LIMIT N → bound = min(N, maximum)。

    外層 LIMIT 一律生效,即使查詢「只在子查詢」寫 LIMIT(舊 regex 版會被繞過)也被
    bound 收斂。內層 ORDER BY / OFFSET / 既有 LIMIT 都保留在子查詢內;子查詢重複欄名
    安全(Postgres 只在「引用」歧義欄位時報錯,derived table 的 SELECT * 重導出不報錯)。

    注意:無 LIMIT 的 ORDER BY 被包進子查詢後,單層 derived table 實務上保排序,但 SQL
    標準不保證最外層順序;若需嚴格全域排序,呼叫端應自帶頂層 ORDER BY。
    """
    top = _top_level_limit(sql)
    bound = default if top is None else min(top, maximum)
    inner = _strip_trailing_semicolons(sql)
    return f"SELECT * FROM (\n{inner}\n) AS _capped\nLIMIT {bound}"


# ---- Output truncation ----------------------------------------------------


def truncate_payload(text: str, maximum: int = MAX_OUTPUT_BYTES) -> str:
    """如果 serialized output 超過 maximum bytes,截斷並附註記。"""
    encoded = text.encode("utf-8")
    if len(encoded) <= maximum:
        return text
    head = encoded[:maximum].decode("utf-8", errors="ignore")
    return (
        f"{head}\n... [truncated: response exceeded {maximum} bytes, "
        f"add narrower WHERE / smaller LIMIT to see the rest]"
    )
