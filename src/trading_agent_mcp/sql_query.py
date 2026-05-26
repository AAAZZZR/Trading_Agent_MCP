"""`execute_readonly_sql` tool 的純函數驗證層。

跟 DB I/O 解耦,方便單元測試。三件事:

1. `validate_select_only(sql)` —— 用 sqlparse 解,只放行 SELECT / WITH ... SELECT,
   拒絕多 statement、拒絕含敏感 function(pg_sleep / copy / lo_import 等)。
2. `clamp_limit(sql)` —— 沒寫 LIMIT 自動加 LIMIT DEFAULT_LIMIT;LIMIT > MAX 改成 MAX。
3. `truncate_payload(text)` —— serialize 後超過 MAX_OUTPUT_BYTES 就截斷加註記。

DB role + sqlparse + statement_timeout = 三層防護。本檔只負責第二層。
"""

from __future__ import annotations

import re

import sqlparse
from sqlparse.sql import Statement
from sqlparse.tokens import DML, Keyword

# ---- 常數 -----------------------------------------------------------------

DEFAULT_LIMIT = 1000
MAX_LIMIT = 10000
MAX_OUTPUT_BYTES = 100 * 1024  # 100KB

# 敏感 function / keyword 黑名單 —— 即使 readonly role 擋 DML,這些 SELECT-context
# 下仍可能炸 DB 或讀檔。比對時轉小寫做 substring 檢查(粗暴但夠用)。
# 注意:做 substring match 會誤殺欄位名(例如有人欄位叫 copy_url),但這層只是
# 第二道防線,真的炸不到 DB(role 擋住);誤殺再放寬。
_BLOCKED_FUNCTIONS = (
    "pg_sleep",
    "pg_read_server_files",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_stat_file",
    "lo_import",
    "lo_export",
    "dblink",
)

# COPY 是 statement 級不是 function,單獨檢查(只有 "COPY tbl ..." 形態)
_BLOCKED_STATEMENTS = ("copy",)


class SQLValidationError(ValueError):
    """SQL 不通過驗證 —— tool 應該回 friendly error 而非 raise 給 LLM。"""


# ---- 驗證 -----------------------------------------------------------------


def validate_select_only(sql: str) -> None:
    """SQL 必須是單一 SELECT(可帶 WITH CTE)。不通過 raise SQLValidationError。

    流程:
      1. sqlparse.parse() 切 statements,移除空白後必須 == 1 個。
      2. 該 statement 第一個有意義的 keyword 必須是 SELECT 或 WITH。
      3. 整串小寫做 blocklist substring 比對(pg_sleep / copy / lo_import 等)。
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

    # 黑名單比對:lower-case substring。複雜情境(例如 identifier 帶 "pg_sleep")
    # 會誤殺,但寧錯殺不放過 —— DB role 是真正的最後防線。
    lowered = sql.lower()
    for blocked in _BLOCKED_FUNCTIONS + _BLOCKED_STATEMENTS:
        if blocked in lowered:
            raise SQLValidationError(f"Use of '{blocked}' is not allowed.")


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

# 抓最外層 LIMIT 數字(忽略大小寫)。粗略 regex:LIMIT <整數>,只用來判斷有無與
# clamp 大小。不處理 LIMIT <expr> 這種非常數情境(罕見,該情境直接放行,反正
# statement_timeout 兜底)。
_LIMIT_RE = re.compile(r"\blimit\s+(\d+)\b", re.IGNORECASE)


def clamp_limit(sql: str, default: int = DEFAULT_LIMIT, maximum: int = MAX_LIMIT) -> str:
    """確保 SQL 帶合理 LIMIT。回傳修改後 SQL。

    - 沒 LIMIT → 在最末(分號前)加 `LIMIT default`。
    - LIMIT N where N > maximum → 改成 LIMIT maximum。
    - LIMIT N where N <= maximum → 不動。

    僅處理 ASCII 整數 LIMIT,不處理 `LIMIT $1` / `LIMIT ALL` 等;這類情境
    視同「沒寫 LIMIT」邏輯不會誤觸發,statement_timeout 兜底。
    """
    match = _LIMIT_RE.search(sql)
    if match is None:
        return _append_limit(sql, default)

    value = int(match.group(1))
    if value <= maximum:
        return sql
    # 用 regex span 精準替換掉那個數字,保留 SQL 其他形態
    start, end = match.span(1)
    return sql[:start] + str(maximum) + sql[end:]


def _append_limit(sql: str, limit: int) -> str:
    """在 SQL 末尾(分號前)加 LIMIT N。"""
    stripped = sql.rstrip()
    if stripped.endswith(";"):
        return f"{stripped[:-1]} LIMIT {limit};"
    return f"{stripped} LIMIT {limit}"


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
