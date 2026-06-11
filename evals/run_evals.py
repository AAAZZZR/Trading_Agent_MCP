"""Agent eval runner —— 拿真實投資問題,讓 Claude 透過本 repo 的 MCP server 實際作答,
再用 LLM judge 對照 rubric 評分。量測「AI agent 能不能正確使用這個資料庫」。

跑法(先決條件:`.env` 已設、claude CLI 已登入):
    uv run python evals/run_evals.py                      # 全部 30 題
    uv run python evals/run_evals.py --max-questions 2    # 冒煙(只跑 2 題)
    uv run python evals/run_evals.py --filter honesty     # 只跑 category / id 含 "honesty" 的題
    uv run python evals/run_evals.py --model claude-sonnet-4-6 --judge-model claude-sonnet-4-6

機制:
    1. 產生臨時 MCP config JSON,指向 `uv run python -m trading_agent_mcp --stdio`。
    2. 每題:claude -p "<question>" --mcp-config <tmp> --allowedTools "mcp__investor-db__*"
       --max-turns N --output-format json --model <model>(subprocess,timeout 300s)。
    3. Judge:再用一次 claude -p(不掛 MCP),給 question + rubric + 受測回答,
       要求只回 JSON {"pass", "score", "reason"}。
    4. 輸出 console 總表 + 完整紀錄寫 evals/results/run_<timestamp>.json。

單題 crash / timeout 記為 error,不中斷整輪。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

# repo 根目錄(evals/ 的上一層)—— MCP config 的 cwd 與輸出路徑都以此為基準。
REPO_ROOT = Path(__file__).resolve().parent.parent
QUESTIONS_PATH = Path(__file__).resolve().parent / "questions.yaml"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

# 受測 agent 的工具白名單:只放本 MCP server 的工具,逼它走資料庫而非內建知識。
ALLOWED_TOOLS = "mcp__investor-db__*"

# 關鍵:claude CLI 的 -p 模式即使指定 --allowedTools,內建工具(WebSearch / WebFetch / Bash /
# Read / …)仍可被 agent 取用 —— 那會變成側門:agent 改去網路 / 本機檔案找答案,測驗就量不到
# 「它能不能正確用這個資料庫」。所以明確 disallow 這些側門工具,逼 agent 只能靠 MCP。
# (尤其誠實題:沒擋掉 WebSearch 的話,agent 會去網路撈台股 / 加密貨幣價格而非如實說「查不到」。)
DISALLOWED_TOOLS = [
    "WebSearch",
    "WebFetch",
    "Bash",
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "PowerShell",
    "NotebookEdit",
    "Agent",
    "Task",
]

# 單次 claude CLI subprocess 上限(秒)。answer 跑多輪工具,給足 300s;judge 較快。
ANSWER_TIMEOUT_S = 300
JUDGE_TIMEOUT_S = 120

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TURNS = 15


# ============================================================
# 資料結構
# ============================================================


@dataclass
class Question:
    id: str
    category: str
    question: str
    rubric: str
    max_turns: int = DEFAULT_MAX_TURNS


@dataclass
class QuestionResult:
    question: Question
    status: str  # "ok" | "error"
    answer: str = ""
    num_turns: int | None = None
    cost_usd: float | None = None
    error: str | None = None
    judge_pass: bool | None = None
    judge_score: float | None = None
    judge_reason: str = ""
    raw_answer_json: dict[str, Any] = field(default_factory=dict)


# ============================================================
# 載入題庫
# ============================================================


def load_questions(path: Path) -> list[Question]:
    """讀 questions.yaml → list[Question]。缺必要欄位直接報錯(不靜默跳過)。"""
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    raw = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path} 沒有非空的 'questions' 清單")

    out: list[Question] = []
    seen_ids: set[str] = set()
    for i, item in enumerate(raw):
        missing = [k for k in ("id", "category", "question", "rubric") if k not in item]
        if missing:
            raise ValueError(f"第 {i} 題缺欄位 {missing}: {item!r}")
        qid = str(item["id"])
        if qid in seen_ids:
            raise ValueError(f"重複的 question id: {qid}")
        seen_ids.add(qid)
        out.append(
            Question(
                id=qid,
                category=str(item["category"]),
                question=str(item["question"]),
                rubric=str(item["rubric"]).strip(),
                max_turns=int(item.get("max_turns", DEFAULT_MAX_TURNS)),
            )
        )
    return out


def filter_questions(
    questions: list[Question], flt: str | None, max_questions: int | None
) -> list[Question]:
    """--filter:id 或 category 子字串(case-insensitive);--max-questions:截斷前 N 題。"""
    out = questions
    if flt:
        needle = flt.lower()
        out = [q for q in out if needle in q.id.lower() or needle in q.category.lower()]
    if max_questions is not None:
        out = out[:max_questions]
    return out


# ============================================================
# 臨時 MCP config
# ============================================================


def write_mcp_config() -> Path:
    """產生臨時 MCP config JSON,讓 claude CLI spawn 本 repo 的 stdio MCP server。"""
    config = {
        "mcpServers": {
            "investor-db": {
                "command": "uv",
                "args": ["run", "python", "-m", "trading_agent_mcp", "--stdio"],
                "cwd": str(REPO_ROOT),
            }
        }
    }
    fd, name = tempfile.mkstemp(prefix="mcp_eval_", suffix=".json")
    with open(fd, "w", encoding="utf-8") as f:
        json.dump(config, f)
    return Path(name)


# ============================================================
# claude CLI 呼叫
# ============================================================


def _find_claude() -> str:
    """找 claude 可執行檔(Windows 上是 claude.exe)。找不到 → 明確報錯。"""
    exe = shutil.which("claude")
    if not exe:
        raise FileNotFoundError(
            "找不到 claude CLI。請先安裝並登入 Claude Code,再重跑 evals。"
        )
    return exe


def _run_cli(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """跑 claude CLI(shell=False + 完整參數 list,Windows 安全)。"""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
    )


def _extract_first_json(text: str) -> dict[str, Any] | None:
    """從文字撈第一個平衡的 JSON object(容錯:judge 可能在 JSON 前後夾雜散文)。"""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break  # 這個 { 起點不可解,換下一個
        start = text.find("{", start + 1)
    return None


def run_answer(
    claude_exe: str, question: Question, mcp_config: Path, model: str
) -> tuple[str, dict[str, Any]]:
    """跑受測 agent。回 (answer_text, raw_result_json)。失敗丟例外給上層記成 error。"""
    args = [
        claude_exe,
        "-p",
        question.question,
        "--mcp-config",
        str(mcp_config),
        "--strict-mcp-config",  # 只用本 config 的 server,避免吃到使用者既有的 MCP
        "--allowedTools",
        ALLOWED_TOOLS,
        "--disallowedTools",
        *DISALLOWED_TOOLS,  # 擋掉內建側門工具(WebSearch / Bash / …),逼 agent 只走 MCP
        "--max-turns",
        str(question.max_turns),
        "--output-format",
        "json",
        "--model",
        model,
    ]
    proc = _run_cli(args, ANSWER_TIMEOUT_S)
    if proc.returncode != 0:
        raise RuntimeError(
            f"answer CLI exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:500]}"
        )
    data = _extract_first_json(proc.stdout)
    if data is None:
        raise RuntimeError(f"answer 輸出非 JSON:{proc.stdout.strip()[:500]}")
    # claude -p --output-format json 的答案在 "result";容錯不同欄位名。
    answer = data.get("result") or data.get("response") or ""
    if data.get("is_error"):
        # agent 端報錯(例如 max-turns 用盡)—— 仍把 result 當回答交給 judge 評。
        answer = answer or f"[agent error: {data.get('subtype') or data.get('terminal_reason')}]"
    return str(answer), data


JUDGE_PROMPT_TEMPLATE = """你是嚴格但公平的評審,評估一個 AI agent 回答投資資料查詢問題的品質。\
這個 agent 透過 MCP 工具查一個美股資料庫(美股 + ADR、約 3-5 年歷史、EOD 非即時、\
財報來自 SEC XBRL、Form 4 內部人、13F 機構持倉、期權 EOD、總經/商品;**沒有**分析師預估/\
目標價、**沒有**即時報價、**沒有**非美上市、**沒有**加密貨幣)。

只依下面的 rubric 評分。資料每天變動,**不要因為你不知道當下的精確數值就扣分**——\
重點是:方向/量級是否合理、是否引用了具體數字與資料日期、是否誠實標示資料侷限。\
對「誠實題」:若 agent 編造了資料庫不該有的數字(分析師目標價、即時報價、台股、加密貨幣、\
未來 EPS 預估等),一律判 fail。

【問題】
{question}

【評分 rubric】
{rubric}

【受測 agent 的回答】
{answer}

請**只輸出一個 JSON object**,不要任何其他文字、不要 markdown code fence,格式嚴格如下:
{{"pass": true 或 false, "score": 0 到 10 的整數或小數, "reason": "一兩句繁體中文說明判定理由"}}
"""


def run_judge(
    claude_exe: str, question: Question, answer: str, judge_model: str
) -> dict[str, Any]:
    """跑 judge(不掛 MCP)。回 {"pass", "score", "reason"};解析失敗回 error 標記。"""
    prompt = JUDGE_PROMPT_TEMPLATE.format(
        question=question.question, rubric=question.rubric, answer=answer or "(空回答)"
    )
    args = [
        claude_exe,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--max-turns",
        "1",
        "--model",
        judge_model,
    ]
    proc = _run_cli(args, JUDGE_TIMEOUT_S)
    if proc.returncode != 0:
        raise RuntimeError(
            f"judge CLI exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:300]}"
        )
    outer = _extract_first_json(proc.stdout)
    judge_text = ""
    if outer is not None:
        judge_text = str(outer.get("result") or "")
    # judge 的 verdict JSON 藏在 outer["result"](claude wrapper)裡;再撈一層。
    verdict = _extract_first_json(judge_text) or _extract_first_json(proc.stdout)
    if verdict is None or "pass" not in verdict:
        raise RuntimeError(f"judge 回傳無法解析為 verdict JSON:{judge_text[:300]}")
    return verdict


def coerce_score(raw: Any) -> float | None:
    """judge 的 score 可能是 int / float / 字串;容錯轉 float。"""
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        m = re.search(r"-?\d+(\.\d+)?", raw)
        if m:
            return float(m.group())
    return None


# ============================================================
# 主流程
# ============================================================


def evaluate_one(
    claude_exe: str,
    question: Question,
    mcp_config: Path,
    model: str,
    judge_model: str,
) -> QuestionResult:
    """跑單題:answer → judge。任何階段例外 → status=error(不中斷整輪)。"""
    try:
        answer, raw = run_answer(claude_exe, question, mcp_config, model)
    except (subprocess.TimeoutExpired, RuntimeError, OSError) as exc:
        return QuestionResult(question=question, status="error", error=f"answer: {exc}")

    res = QuestionResult(
        question=question,
        status="ok",
        answer=answer,
        num_turns=raw.get("num_turns"),
        cost_usd=raw.get("total_cost_usd"),
        raw_answer_json=raw,
    )
    try:
        verdict = run_judge(claude_exe, question, answer, judge_model)
        res.judge_pass = bool(verdict.get("pass"))
        res.judge_score = coerce_score(verdict.get("score"))
        res.judge_reason = str(verdict.get("reason", ""))
    except (subprocess.TimeoutExpired, RuntimeError, OSError) as exc:
        res.status = "error"
        res.error = f"judge: {exc}"
    return res


def print_question_line(idx: int, total: int, res: QuestionResult) -> None:
    """逐題印一行:PASS / FAIL / ERROR + 簡短理由。"""
    q = res.question
    if res.status == "error":
        tag = "ERROR"
        detail = res.error or ""
    else:
        tag = "PASS" if res.judge_pass else "FAIL"
        score = f"{res.judge_score:g}" if res.judge_score is not None else "?"
        turns = res.num_turns if res.num_turns is not None else "?"
        detail = f"score={score} turns={turns} | {res.judge_reason}"
    print(f"[{idx}/{total}] {tag:5s} {q.id} ({q.category})")
    if detail:
        print(f"        {detail}")


def summarize(results: list[QuestionResult]) -> dict[str, Any]:
    """彙整 per-category 通過率與平均 turns。"""
    by_cat: dict[str, list[QuestionResult]] = defaultdict(list)
    for r in results:
        by_cat[r.question.category].append(r)

    cat_summary: dict[str, Any] = {}
    for cat, items in sorted(by_cat.items()):
        passed = sum(1 for r in items if r.status == "ok" and r.judge_pass)
        errored = sum(1 for r in items if r.status == "error")
        turns = [r.num_turns for r in items if r.num_turns is not None]
        avg_turns = round(sum(turns) / len(turns), 1) if turns else None
        cat_summary[cat] = {
            "total": len(items),
            "passed": passed,
            "errored": errored,
            "pass_rate": round(passed / len(items), 3) if items else 0.0,
            "avg_turns": avg_turns,
        }

    total = len(results)
    total_passed = sum(1 for r in results if r.status == "ok" and r.judge_pass)
    total_errored = sum(1 for r in results if r.status == "error")
    return {
        "total": total,
        "passed": total_passed,
        "errored": total_errored,
        "pass_rate": round(total_passed / total, 3) if total else 0.0,
        "by_category": cat_summary,
    }


def print_summary(summary: dict[str, Any]) -> None:
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"{'category':<18}{'pass':>6}{'total':>7}{'rate':>8}{'avg_turns':>11}")
    for cat, s in summary["by_category"].items():
        rate = f"{s['pass_rate'] * 100:.0f}%"
        at = s["avg_turns"] if s["avg_turns"] is not None else "-"
        print(f"{cat:<18}{s['passed']:>6}{s['total']:>7}{rate:>8}{at!s:>11}")
    print("-" * 60)
    overall_rate = f"{summary['pass_rate'] * 100:.0f}%"
    print(f"{'OVERALL':<18}{summary['passed']:>6}{summary['total']:>7}{overall_rate:>8}")
    if summary["errored"]:
        print(f"\n{summary['errored']} 題 error(未完成,不計入通過)。")


def write_results(
    results: list[QuestionResult], summary: dict[str, Any], meta: dict[str, Any]
) -> Path:
    """完整紀錄(含每題回答與 judge 理由)寫 results/run_<timestamp>.json。"""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = RESULTS_DIR / f"run_{ts}.json"
    payload = {
        "meta": meta,
        "summary": summary,
        "results": [
            {
                "id": r.question.id,
                "category": r.question.category,
                "question": r.question.question,
                "rubric": r.question.rubric,
                "status": r.status,
                "error": r.error,
                "num_turns": r.num_turns,
                "cost_usd": r.cost_usd,
                "judge_pass": r.judge_pass,
                "judge_score": r.judge_score,
                "judge_reason": r.judge_reason,
                "answer": r.answer,
            }
            for r in results
        ],
    }
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return out_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Agent eval runner —— 讓 Claude 透過 MCP 作答,LLM judge 評分。"
    )
    p.add_argument(
        "--filter", default=None, help="只跑 id / category 含此子字串的題(case-insensitive)。"
    )
    p.add_argument(
        "--model", default=DEFAULT_MODEL, help=f"受測 agent 的 model(預設 {DEFAULT_MODEL})。"
    )
    p.add_argument(
        "--judge-model",
        default=None,
        help="judge 的 model(預設同 --model)。",
    )
    p.add_argument(
        "--max-questions",
        type=int,
        default=None,
        help="最多跑前 N 題(冒煙測試用,例 --max-questions 2)。",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    judge_model = args.judge_model or args.model

    questions = load_questions(QUESTIONS_PATH)
    selected = filter_questions(questions, args.filter, args.max_questions)
    if not selected:
        print(f"沒有題目符合 --filter={args.filter!r}。", file=sys.stderr)
        return 2

    try:
        claude_exe = _find_claude()
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 3

    print(f"題數:{len(selected)}  model:{args.model}  judge:{judge_model}")
    print(f"MCP server cwd:{REPO_ROOT}\n")

    mcp_config = write_mcp_config()
    results: list[QuestionResult] = []
    try:
        for i, q in enumerate(selected, start=1):
            res = evaluate_one(claude_exe, q, mcp_config, args.model, judge_model)
            results.append(res)
            print_question_line(i, len(selected), res)
    finally:
        mcp_config.unlink(missing_ok=True)

    summary = summarize(results)
    print_summary(summary)

    meta = {
        "timestamp": datetime.now().isoformat(),
        "model": args.model,
        "judge_model": judge_model,
        "filter": args.filter,
        "max_questions": args.max_questions,
        "repo_root": str(REPO_ROOT),
    }
    out_path = write_results(results, summary, meta)
    print(f"\n完整紀錄:{out_path}")

    # 有 error 但不失敗整個 process;全跑完回 0,讓 CI / 人工看通過率自行判斷。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
