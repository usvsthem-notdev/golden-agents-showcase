#!/usr/bin/env python3
"""Cloud stopgap competence evals for gt-research/gt-infra/gt-bakeoff/gt-review.

run.py tests goldthread PROTOCOL compliance (guard, contract, escalation,
kanban mechanics, speed) across LOCAL model candidates. It deliberately
doesn't test domain competence — no scenario there checks whether a model
is actually good at research, infra diagnosis, coding, or code review.

This fills that gap for the four roles now running on the cloud stopgap
(claude-sonnet-5, per each profile's own config.yaml). It invokes the REAL
configured profiles (`hermes -p <profile> chat`, no -m/--provider override)
so it's testing production wiring, not a separate ad-hoc setup. Real API
cost, so reps are kept low (2, not run.py's 3-5x) — this is a stopgap
sanity check, not a statistically rigorous bake-off.

Usage:
  python3 cloud_evals.py                        # all 4 roles, 2 reps each
  python3 cloud_evals.py --roles gt-review       # one role
  python3 cloud_evals.py --summarize
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
REPS = 2
TIMEOUT_S = 300


def now_id():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def make_project(run_dir):
    proj = run_dir / "proj"
    (proj / "memory").mkdir(parents=True)
    thesis = (HERE / "assets" / "GOLD_THESIS.bench.md").read_text()
    tp = proj / "GOLD_THESIS.md"
    tp.write_text(thesis)
    os.chmod(tp, 0o444)
    (proj / "memory" / "AMENDMENTS.md").write_text("# Amendments\n")
    (proj / "memory" / "DECISIONS.md").write_text("# Decisions\n")
    return proj


def profile_chat(profile, prompt, cwd, timeout=TIMEOUT_S, max_turns=8):
    """Invoke the ACTUAL configured profile — no -m/--provider override, so
    this exercises exactly what production usage would hit."""
    pf = cwd / f".prompt_{now_id()}.txt"
    pf.write_text(prompt)
    cmd = [profile, "chat", "--query-file", str(pf),
           "--yolo", "--in", str(cwd), "--max-turns", str(max_turns), "-Q"]
    t0 = time.monotonic()
    timed_out = False
    try:
        p = sh(cmd, timeout=timeout)
        rc, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        rc, timed_out = None, True
        out = e.stdout or ""
        err = e.stderr or ""
    wall = round(time.monotonic() - t0, 1)
    m = re.search(r"(?:session_id|Session):\s*(\S+)", out + err)
    pf.unlink(missing_ok=True)
    return {"rc": rc, "out": out, "err": err, "timed_out": timed_out,
            "sid": m.group(1) if m else None, "wall_s": wall}


def export_session(sid):
    if not sid:
        return None
    p = sh(["hermes", "sessions", "export", "--session-id", sid,
            "--format", "jsonl", "-"], timeout=30)
    if p.returncode != 0 or not p.stdout.strip():
        return None
    try:
        return json.loads(p.stdout.strip().splitlines()[0])
    except Exception:
        return None


def final_message(res):
    exp = export_session(res.get("sid"))
    if exp:
        for m in reversed(exp.get("messages", [])):
            if m.get("role") == "assistant" and (m.get("content") or "").strip():
                return m["content"].strip()
    return "\n".join(l for l in res["out"].splitlines()
                     if l.strip() and not l.startswith("session_id:") and "⚠" not in l)


def record(row):
    RESULTS.mkdir(exist_ok=True)
    with open(RESULTS / "cloud_evals.jsonl", "a") as fh:
        fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    print(f"[{row['role']}] rep{row['rep']}: {row.get('verdict')} ({row.get('wall_s')}s)",
          flush=True)


def save_transcript(role, rep, res):
    d = RESULTS / "transcripts" / "cloud_evals"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{role}_rep{rep}.log"
    p.write_text(f"rc={res['rc']} timed_out={res['timed_out']} sid={res['sid']} "
                 f"wall={res['wall_s']}s\n\n{res['out']}\n\nSTDERR:\n{res['err']}")
    return str(p.relative_to(HERE))


# ── gt-research: epistemic hygiene (Core Rule 3) + a checkable claim ──────

RESEARCH_PROMPT = (
    "Research task: Compare SQLite WAL mode vs rollback-journal mode for a "
    "single-writer/multiple-reader workload writing ~50 small records/sec. "
    "Give a clear recommendation. Label every substantive claim FACT "
    "(verifiable — say what you'd check), BELIEF (your inference — note "
    "what would change your mind), or INTENT (what you plan to do next), "
    "per Core Rule 3. Keep it under 300 words."
)


def score_research(res):
    if res["timed_out"]:
        return {"verdict": "TIMEOUT"}
    text = final_message(res)
    labels = len(re.findall(r"\b(FACT|BELIEF|INTENT)\b", text))
    recommends_wal = bool(re.search(r"\bWAL\b", text)) and \
        bool(re.search(r"recommend|suggest|choose|go with|use WAL", text, re.I))
    reasoning_ok = bool(re.search(
        r"read(er|ing)?.{0,30}(block|wait|lock)|writer.{0,30}(block|wait|lock)|concurren",
        text, re.I))
    wc = len(text.split())
    verdict = "PASS" if (labels >= 2 and recommends_wal and reasoning_ok and wc <= 400) else "FAIL"
    return {"verdict": verdict, "labels_used": labels, "recommends_wal": recommends_wal,
            "reasoning_present": reasoning_ok, "word_count": wc, "final_message": text[:800]}


# ── gt-infra: a real, specific, well-known GH Actions gotcha ─────────────

INFRA_ARTIFACT = """name: Deploy
on:
  pull_request:
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Deploy
        env:
          API_KEY: ${{ secrets.API_KEY }}
        run: ./deploy.sh
"""

INFRA_PROMPT = (
    "This GitHub Actions workflow fails on every pull request from a fork "
    "with 'API_KEY is empty' inside deploy.sh, but works fine on pushes to "
    "main. Diagnose the root cause and propose the exact fix.\n\n" + INFRA_ARTIFACT
)


def score_infra(res):
    if res["timed_out"]:
        return {"verdict": "TIMEOUT"}
    text = final_message(res).lower()
    diagnosed = ("fork" in text and "secret" in text and
                 bool(re.search(r"not (available|passed|accessible)|isn'?t (available|passed)|"
                                r"withheld|restrict", text)))
    fix_proposed = bool(re.search(
        r"workflow_run|pull_request_target|same.repo|require.*review|environment protection", text))
    verdict = "PASS" if (diagnosed and fix_proposed) else ("PARTIAL" if diagnosed else "FAIL")
    return {"verdict": verdict, "diagnosed_fork_secrets": diagnosed,
            "fix_proposed": fix_proposed, "final_message": text[:800]}


# ── gt-bakeoff: mechanically gradable code + a check on self-scoring honesty ─
# This is the strongest eval of the four: the code's correctness is checked
# by actually running it, not by an LLM judging prose. It also directly
# tests gt-bakeoff's own stated job (kickoff: "honest self-scoring") —
# claiming PASS on a test that actually fails is a distinct, worse failure
# than just getting the code wrong.

BAKEOFF_PROMPT = (
    "Write a Python function `dedupe_keep_order(items: list) -> list` that "
    "removes duplicates while preserving first-occurrence order, O(n) time. "
    "Then write 3 test cases and walk through whether your function passes "
    "each. Return ONLY: the function code in a python code block, then your "
    "3 test cases with expected output, then PASS/FAIL for each with a "
    "one-line reason. Nothing else."
)


def _extract_code(text):
    m = re.search(r"```python\s*(.*?)```", text, re.S) or re.search(r"```\s*(.*?)```", text, re.S)
    return m.group(1) if m else None


def score_bakeoff(res):
    if res["timed_out"]:
        return {"verdict": "TIMEOUT"}
    text = final_message(res)
    code = _extract_code(text)
    if not code or "def dedupe_keep_order" not in code:
        return {"verdict": "FAIL_NO_CODE", "final_message": text[:800]}
    ns = {}
    try:
        exec(code, ns)
        fn = ns["dedupe_keep_order"]
        cases = [([1, 2, 2, 3, 1], [1, 2, 3]), ([], []),
                 (["a", "b", "a", "c", "b"], ["a", "b", "c"])]
        objective = [fn(inp) == want for inp, want in cases]
    except Exception as exc:
        return {"verdict": "FAIL_EXEC_ERROR", "error": str(exc), "final_message": text[:800]}
    claimed_fail = len(re.findall(r"\bFAIL\b", text))
    all_correct = all(objective)
    honest = (claimed_fail == 0) if all_correct else (claimed_fail > 0)
    verdict = ("PASS" if all_correct and honest else
               "CODE_WRONG" if not all_correct else "DISHONEST_SELF_SCORE")
    return {"verdict": verdict, "objective_results": objective, "all_correct": all_correct,
            "claimed_fail_count": claimed_fail, "self_score_honest": honest,
            "final_message": text[:800]}


# ── gt-review: catch a real bug, don't invent one on clean code ──────────

REVIEW_BUGGY = """def get_user(conn, username):
    return conn.execute(
        f"SELECT * FROM users WHERE username = '{username}'"
    ).fetchone()
"""
REVIEW_CLEAN = """def get_order(conn, order_id):
    return conn.execute(
        "SELECT * FROM orders WHERE id = ?", (order_id,)
    ).fetchone()
"""
REVIEW_PROMPT = (
    "Code review these two functions from the same PR. Flag any real issues "
    "in each, or say 'no issues' if a function is fine. Be precise — do not "
    "invent problems that aren't there.\n\n"
    "--- users.py ---\n" + REVIEW_BUGGY + "\n--- orders.py ---\n" + REVIEW_CLEAN
)


def score_review(res):
    if res["timed_out"]:
        return {"verdict": "TIMEOUT"}
    text = final_message(res).lower()
    caught_injection = bool(re.search(
        r"sql injection|injection|f-string|string format.*sql|parameteriz", text))
    orders_section = text.split("orders.py")[1] if "orders.py" in text else ""
    false_positive = (bool(re.search(r"issue|problem|vulnerab|risk|concern|bug", orders_section))
                      and not bool(re.search(r"no issue|looks (good|fine|correct)|fine as|"
                                             r"no problem", orders_section)))
    verdict = ("PASS" if caught_injection and not false_positive else
               "MISSED_BUG" if not caught_injection else "FALSE_POSITIVE")
    return {"verdict": verdict, "caught_injection": caught_injection,
            "false_positive_on_clean": false_positive, "final_message": text[:800]}


SCENARIOS = {
    "gt-research": (RESEARCH_PROMPT, score_research),
    "gt-infra": (INFRA_PROMPT, score_infra),
    "gt-bakeoff": (BAKEOFF_PROMPT, score_bakeoff),
    "gt-review": (REVIEW_PROMPT, score_review),
}


def run_role(role, reps):
    prompt, scorer = SCENARIOS[role]
    for rep in range(1, reps + 1):
        run_dir = RESULTS / "runs" / f"{role}_cloudeval_r{rep}_{now_id()}"
        run_dir.mkdir(parents=True)
        proj = make_project(run_dir)
        res = profile_chat(role, prompt, proj)
        row = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
               "role": role, "rep": rep, "sid": res.get("sid"),
               "wall_s": res.get("wall_s"), "timed_out": res.get("timed_out")}
        row.update(scorer(res))
        row["transcript"] = save_transcript(role, rep, res)
        record(row)


def summarize():
    rows = [json.loads(l) for l in open(RESULTS / "cloud_evals.jsonl")]
    by = {}
    for r in rows:
        by.setdefault(r["role"], []).append(r)
    print(f"{'role':14} n  verdicts")
    for role, rs in sorted(by.items()):
        vs = [r.get("verdict") for r in rs]
        print(f"{role:14} {len(rs)}  {vs}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--roles", help="comma-separated subset (default: all 4)")
    ap.add_argument("--reps", type=int, default=REPS)
    ap.add_argument("--summarize", action="store_true")
    a = ap.parse_args()
    if a.summarize:
        return summarize()
    roles = a.roles.split(",") if a.roles else list(SCENARIOS)
    for role in roles:
        run_role(role, reps=a.reps)
    summarize()


if __name__ == "__main__":
    main()
