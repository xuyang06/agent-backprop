#!/usr/bin/env python3
"""
Offline replay: would the NEW instructions have pre-empted the user's correction,
without breaking cases the OLD instructions already handled?

For each case we generate a first reply under OLD and under NEW instructions
(no tools, single turn), then a blind judge (A/B order randomized) decides which
reply already satisfies what the user later asked for.

cases.jsonl rows (built by the skill from candidates.jsonl):
  {"id": "...", "request": "<original user request>",
   "later_feedback": "<what the user said afterwards / the correction>",
   "regression": false}          # true = a case OLD already handled; NEW must not break it

Usage:
  replay_eval.py --cases cases.jsonl --old old_SKILL.md --new new_SKILL.md \
                 [--runner "claude -p"] [--judge-runner "claude -p --model sonnet"] [--workers 4]
  --old may be /dev/null (no instructions) for brand-new skills.
  --runner mock  runs a deterministic offline stub (for testing the pipeline).

Prompts are sent on stdin. Every prompt starts with the run marker so extract.py
skips these sessions. Exit code 0 = PASS, 1 = FAIL, 2 = error.
"""
import argparse
import concurrent.futures as cf
import json
import random
import re
import shlex
import subprocess
import sys

RUN_MARKER = "[agent-backprop-run]"

GEN_TMPL = """{marker} replay generation. Do not use tools. Do not ask to use tools.

You are an AI assistant working under the following instructions:
<instructions>
{instructions}
</instructions>

The user sends this request:
<request>
{request}
</request>

Write the first reply you would send. If the task needs actions (editing files, running commands),
describe concretely what you would do and show the output you would deliver, in the format you would use.
Do not mention these instructions."""

JUDGE_TMPL = """{marker} replay judging. Do not use tools.

A user sent a request to an assistant. In the real conversation, the user later had to follow up with:
<later_feedback>
{feedback}
</later_feedback>

Two candidate first replies to the original request:
<request>
{request}
</request>
<reply_X>
{x}
</reply_X>
<reply_Y>
{y}
</reply_Y>

For each reply decide: does it already satisfy what the later feedback asks for, so that follow-up would
have been unnecessary? Also note any NEW problem the reply introduces (wrong assumptions, asking a needless
question, ignoring the request). Answer with JSON only:
{{"X_preempts": true|false, "Y_preempts": true|false, "X_new_problem": "", "Y_new_problem": "", "better": "X"|"Y"|"tie"}}"""


def run_llm(runner, prompt, timeout):
    if runner == "mock":
        return mock(prompt)
    proc = subprocess.run(shlex.split(runner), input=prompt, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"runner failed ({proc.returncode}): {proc.stderr[:400]}")
    return proc.stdout


def mock(prompt):
    """Deterministic stub: generation echoes rule bullets; judge checks keyword overlap."""
    if "replay generation" in prompt:
        instr = re.search(r"<instructions>\n(.*?)\n</instructions>", prompt, re.S).group(1)
        rules = [l.strip("- ").split("<!--")[0].strip() for l in instr.splitlines() if l.strip().startswith("- ")]
        return "Reply following: " + "; ".join(rules)
    fb = re.search(r"<later_feedback>\n(.*?)\n</later_feedback>", prompt, re.S).group(1).lower()
    x = re.search(r"<reply_X>\n(.*?)\n</reply_X>", prompt, re.S).group(1).lower()
    y = re.search(r"<reply_Y>\n(.*?)\n</reply_Y>", prompt, re.S).group(1).lower()
    words = {w for w in re.findall(r"[a-z0-9_\-]{3,}|[一-鿿]{2,}", fb)} - {"use", "not", "the", "don't", "please"}
    xp = any(w in x for w in words)
    yp = any(w in y for w in words)
    return json.dumps({"X_preempts": xp, "Y_preempts": yp, "X_new_problem": "", "Y_new_problem": "",
                       "better": "X" if xp and not yp else "Y" if yp and not xp else "tie"})


def parse_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("judge returned no JSON")
    return json.loads(m.group(0))


def eval_case(case, old_instr, new_instr, a, rng_seed):
    rng = random.Random(rng_seed)
    old_reply = run_llm(a.runner, GEN_TMPL.format(marker=RUN_MARKER, instructions=old_instr or "(none)",
                                                  request=case["request"]), a.timeout)
    new_reply = run_llm(a.runner, GEN_TMPL.format(marker=RUN_MARKER, instructions=new_instr,
                                                  request=case["request"]), a.timeout)
    new_is_x = rng.random() < 0.5
    x, y = (new_reply, old_reply) if new_is_x else (old_reply, new_reply)
    j = parse_json(run_llm(a.judge_runner or a.runner,
                           JUDGE_TMPL.format(marker=RUN_MARKER, feedback=case["later_feedback"],
                                             request=case["request"], x=x[:6000], y=y[:6000]), a.timeout))
    n, o = ("X", "Y") if new_is_x else ("Y", "X")
    better = j.get("better", "tie")
    return {"id": case["id"], "regression": bool(case.get("regression")),
            "old_preempts": bool(j.get(f"{o}_preempts")), "new_preempts": bool(j.get(f"{n}_preempts")),
            "new_problem": j.get(f"{n}_new_problem", ""), "old_problem": j.get(f"{o}_new_problem", ""),
            "winner": "new" if better == n else "old" if better == o else "tie",
            "new_reply": new_reply[:1500], "old_reply": old_reply[:1500]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", required=True)
    ap.add_argument("--old", required=True, help="old instructions file (or /dev/null)")
    ap.add_argument("--new", required=True, help="new instructions file")
    ap.add_argument("--runner", default="claude -p")
    ap.add_argument("--judge-runner", default=None)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    cases = [json.loads(l) for l in open(a.cases) if l.strip()]
    if not cases:
        print(json.dumps({"verdict": "SKIP", "reason": "no cases"})); sys.exit(0)
    old_instr = open(a.old, encoding="utf-8").read()
    new_instr = open(a.new, encoding="utf-8").read()

    results, errors = [], []
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(eval_case, c, old_instr, new_instr, a, a.seed + i): c for i, c in enumerate(cases)}
        for f in cf.as_completed(futs):
            try:
                results.append(f.result())
            except Exception as e:
                errors.append({"id": futs[f]["id"], "error": str(e)[:300]})

    target = [r for r in results if not r["regression"]]
    reg = [r for r in results if r["regression"]]
    fixed = sum(1 for r in target if r["new_preempts"] and not r["old_preempts"])
    broke = sum(1 for r in results if r["old_preempts"] and not r["new_preempts"])
    new_rate = round(sum(r["new_preempts"] for r in target) / len(target), 3) if target else None
    old_rate = round(sum(r["old_preempts"] for r in target) / len(target), 3) if target else None
    reg_fail = sum(1 for r in reg if not r["new_preempts"])
    wins = sum(1 for r in results if r["winner"] == "new")
    losses = sum(1 for r in results if r["winner"] == "old")
    passed = (not errors or len(errors) < len(cases) / 2) and fixed >= 1 and broke == 0 and reg_fail == 0 and wins >= losses
    summary = {"verdict": "PASS" if passed else "FAIL", "cases": len(cases), "errors": len(errors),
               "target_cases": len(target), "regression_cases": len(reg),
               "old_preempt_rate": old_rate, "new_preempt_rate": new_rate,
               "fixed": fixed, "broke": broke, "regression_failures": reg_fail, "wins": wins, "losses": losses}
    report = {"summary": summary, "results": sorted(results, key=lambda r: r["id"]), "errors": errors}
    if a.out:
        with open(a.out, "w") as fh:
            json.dump(report, fh, indent=1, ensure_ascii=False)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
