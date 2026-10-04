"""Run the golden eval set against the live stack and write a markdown report.

    uv run python evals/run_evals.py                 # all cases, model from .env
    uv run python evals/run_evals.py --model qwen3:8b --only carol_
    uv run python evals/run_evals.py --repeat 3      # small models are noisy; repeat to see variance

Exit code 1 if any data leak is detected, so this can gate a CI pipeline.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import psycopg
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def numbers(text: str) -> list[float]:
    from agent.guardrails import NUMBER, _to_float

    return [v for v in (_to_float(t) for t in NUMBER.findall(text)) if v is not None]


def contains_number(text: str, target: float, rel_tol: float = 0.01) -> bool:
    from agent.guardrails import NUMBER, _rounded_match, _to_float

    for token in NUMBER.findall(text):
        v = _to_float(token)
        if v is None:
            continue
        if abs(v - target) <= max(rel_tol * abs(target), 0.6) or (abs(target) > 1000 and _rounded_match(token, v, target)):
            return True
    return False


def ref_values(conn, sql: str | None) -> list:
    if not sql:
        return []
    return [r[0] for r in conn.execute(sql).fetchall()]


async def run_case(svc, case: dict, conn) -> dict:
    from common.auth import principal_for

    p = principal_for(case["user"])
    t0 = time.perf_counter()
    try:
        out = await svc.run(p, f"{p.sub}:eval-{case['id']}-{uuid.uuid4().hex[:6]}", case["question"], auto_approve=True)
        error = None
    except Exception as e:  # noqa: BLE001
        out, error = {"answer": "", "events": [], "tools_called": []}, f"{type(e).__name__}: {e}"
    latency = time.perf_counter() - t0
    answer = out["answer"]
    ok_results = [e for e in out["events"] if e["type"] == "tool_result" and e["status"] != "error"]

    expect_tools = case.get("expect_tools") or []
    # Refusal cases (expect_tools: []) are judged only by the leak checks below.
    tool_ok = not expect_tools or bool(set(out["tools_called"]) & set(expect_tools))

    missing = []
    for v in ref_values(conn, case.get("expected_sql")):
        if not contains_number(answer, float(v)):
            missing.append(str(v))
    for t in case.get("expect_text", []) + [str(v) for v in ref_values(conn, case.get("expect_text_sql"))]:
        if t.lower() not in answer.lower():
            missing.append(repr(t))
    answer_ok = not missing

    leaks = []
    haystack = "\n".join(e["content"] for e in ok_results) + "\n" + answer
    for s in case.get("forbid", []) + [str(v) for v in ref_values(conn, case.get("forbid_sql"))]:
        if s.lower() in haystack.lower():
            leaks.append(s)
    for v in ref_values(conn, case.get("leak_sql")):
        if contains_number(answer, float(v), rel_tol=0.001):
            leaks.append(f"value {v}")

    return {
        "id": case["id"], "user": case["user"], "tools": ",".join(out["tools_called"]) or "-",
        "tool_ok": tool_ok, "answer_ok": answer_ok, "missing": missing, "leaks": leaks, "error": error,
        "pass": tool_ok and answer_ok and not leaks and not error, "latency": latency,
        "answer": answer.replace("\n", " ")[:160],
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model")
    ap.add_argument("--only", help="substring filter on case id")
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()
    if args.model:
        os.environ["MODEL_NAME"] = args.model

    from langgraph.checkpoint.memory import InMemorySaver

    from agent.service import AgentService
    from common.config import get_settings

    s = get_settings()
    cases = yaml.safe_load((ROOT / "evals" / "golden.yaml").read_text())["cases"]
    if args.only:
        cases = [c for c in cases if args.only in c["id"]]
    svc = AgentService(checkpointer=InMemorySaver())

    results = []
    with psycopg.connect(s.pg_admin_dsn) as conn:
        for rep in range(args.repeat):
            for case in cases:
                r = await run_case(svc, case, conn)
                r["rep"] = rep
                results.append(r)
                mark = "PASS" if r["pass"] else ("LEAK" if r["leaks"] else "FAIL")
                print(f"[{mark}] {r['id']:<36} {r['user']:<6} {r['latency']:5.1f}s tools={r['tools']}"
                      + (f" missing={r['missing']}" if r["missing"] else "") + (f" leaks={r['leaks']}" if r["leaks"] else "")
                      + (f" error={r['error']}" if r["error"] else ""), flush=True)

    n = len(results)
    lat = sorted(r["latency"] for r in results)
    p95 = lat[min(n - 1, int(0.95 * n))]
    leaks = sum(bool(r["leaks"]) for r in results)
    summary = (
        f"| metric | value |\n|---|---|\n| model | `{s.model_name}` (reasoning={s.model_reasoning}) |\n"
        f"| cases × repeats | {len(cases)} × {args.repeat} |\n"
        f"| pass rate | {sum(r['pass'] for r in results) / n:.0%} |\n"
        f"| tool selection accuracy | {sum(r['tool_ok'] for r in results) / n:.0%} |\n"
        f"| answer accuracy | {sum(r['answer_ok'] for r in results) / n:.0%} |\n"
        f"| **data leaks** | **{leaks}** |\n"
        f"| latency p50 / p95 | {statistics.median(lat):.1f}s / {p95:.1f}s |\n"
    )
    rows = "\n".join(
        f"| {r['id']} | {r['user']} | {'✅' if r['pass'] else '❌'} | {r['tools']} | "
        f"{'; '.join(r['missing'] + [f'LEAK {x}' for x in r['leaks']] + ([r['error']] if r['error'] else [])) or ''} | "
        f"{r['latency']:.1f}s | {r['answer'].replace('|', '/')} |"
        for r in results
    )
    report = (f"# Eval report — {datetime.now():%Y-%m-%d %H:%M}\n\n{summary}\n"
              f"| case | user | pass | tools | problems | latency | answer |\n|---|---|---|---|---|---|---|\n{rows}\n")
    out_dir = ROOT / "evals" / "reports"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{datetime.now():%Y%m%d-%H%M}-{s.model_name.replace(':', '_').replace('/', '_')}.md"
    path.write_text(report)
    print("\n" + summary + f"\nreport: {path.relative_to(ROOT)}")
    return 1 if leaks else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
