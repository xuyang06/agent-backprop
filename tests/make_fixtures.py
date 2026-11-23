#!/usr/bin/env python3
"""Build a fake HOME with Claude Code + Codex session logs and a git repo."""
import json, os, sys, shutil, datetime as dt

H = sys.argv[1]
shutil.rmtree(H, ignore_errors=True)
repo = os.path.join(H, "code", "acme")
os.makedirs(os.path.join(repo, ".git"))
open(os.path.join(repo, ".git", "config"), "w").write('[remote "origin"]\n\turl = git@github.com:yang/acme.git\n')
os.makedirs(os.path.join(repo, "src"))
os.makedirs(os.path.join(repo, ".claude", "skills", "py-setup"))
open(os.path.join(repo, ".claude", "skills", "py-setup", "SKILL.md"), "w").write(
    "---\nname: py-setup\ndescription: Set up Python envs for acme. Use for dependency installs.\n---\n\n# Py setup\n\nInstall deps.\n")
os.makedirs(os.path.join(H, ".claude", "skills", "report-writer"))
open(os.path.join(H, ".claude", "skills", "report-writer", "SKILL.md"), "w").write(
    "---\nname: report-writer\ndescription: Write weekly reports\n---\n\nWrite reports.\n")

base = dt.datetime(2026, 11, 20, 18, 0, tzinfo=dt.timezone.utc)
def ts(m): return (base + dt.timedelta(minutes=m)).isoformat().replace("+00:00", "Z")

# ---------------- Claude Code ----------------
cdir = os.path.join(H, ".claude", "projects", "-x-code-acme")
os.makedirs(cdir)
cwd = os.path.join(repo, "src")
def u(text, m, **kw): return {"type": "user", "cwd": cwd, "sessionId": "c1", "gitBranch": "main", "timestamp": ts(m),
                              "message": {"role": "user", "content": text}, **kw}
def asst(content, m): return {"type": "assistant", "cwd": cwd, "sessionId": "c1", "timestamp": ts(m),
                              "message": {"role": "assistant", "content": content}}
def tr(tid, out, err, m): return {"type": "user", "cwd": cwd, "sessionId": "c1", "timestamp": ts(m),
                                  "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": out, "is_error": err}]}}
c1 = [
    {"type": "summary", "summary": "x"},
    u("<command-name>/py-setup</command-name><command-args>install deps</command-args>", 0),
    asst([{"type": "tool_use", "id": "s1", "name": "Skill", "input": {"skill": "py-setup"}}], 1),
    asst([{"type": "text", "text": "I'll install with pip."}, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pip install -r requirements.txt"}}], 2),
    tr("t1", "error: externally-managed-environment", True, 3),
    asst([{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pip install --user -r requirements.txt"}}], 4),
    tr("t2", "ERROR: could not find a version", True, 5),
    asst([{"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "uv sync"}}], 6),
    tr("t3", "Resolved 12 packages", False, 7),
    asst([{"type": "text", "text": "Done! All dependencies are installed and tests pass."}], 8),
    u("no, use uv not pip from the start. and my token is sk-test-FAKEFAKEFAKEFAKE0000", 9),
    asst([{"type": "text", "text": "You're right, I should have used uv. Should I also update the README?"}], 10),
    u("yes", 11),
    u("[Request interrupted by user]", 12),
    u("不对，README 不用改，改 CONTRIBUTING.md", 13),
    asst([{"type": "text", "text": "好的，已修改 CONTRIBUTING.md。"}], 14),
]
with open(os.path.join(cdir, "c1.jsonl"), "w") as f:
    for r in c1: f.write(json.dumps(r) + "\n")
# a agent-backprop run session -> must be skipped
with open(os.path.join(cdir, "c2.jsonl"), "w") as f:
    f.write(json.dumps(u("[agent-backprop-run] Use the agent-backprop skill in daily mode.", 30)) + "\n")
    f.write(json.dumps(asst([{"type": "text", "text": "no, wrong"}], 31)) + "\n")
# subagent
with open(os.path.join(cdir, "agent-abc.jsonl"), "w") as f:
    f.write(json.dumps(u("sub task", 40)) + "\n")

# ---------------- Codex ----------------
xdir = os.path.join(H, ".codex", "sessions", "2026", "11", "20")
os.makedirs(xdir)
def env(t, p, m): return {"timestamp": ts(m), "type": t, "payload": p}
x1 = [
    env("session_meta", {"id": "x1", "cwd": repo, "git": {"repository_url": "https://github.com/yang/acme.git", "branch": "main"}}, 0),
    env("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>cwd</environment_context>"}]}, 0),
    env("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "write the weekly report $report-writer"}]}, 1),
    env("event_msg", {"type": "user_message", "message": "write the weekly report $report-writer"}, 1),
    env("response_item", {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Which format do you want, markdown or docx?"}]}, 2),
    env("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "markdown"}]}, 3),
    env("event_msg", {"type": "user_message", "message": "markdown"}, 3),
    env("response_item", {"type": "function_call", "name": "shell", "arguments": json.dumps({"command": ["cat", "/Users/y/.agents/skills/report-writer/SKILL.md"]}), "call_id": "k1"}, 4),
    env("response_item", {"type": "function_call_output", "call_id": "k1", "output": json.dumps({"output": "...", "metadata": {"exit_code": 0}})}, 4),
    env("response_item", {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Here is the report with emoji headers 🚀."}]}, 5),
    env("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "don't use emoji, I said this last week too"}]}, 6),
    env("event_msg", {"type": "user_message", "message": "don't use emoji, I said this last week too"}, 6),
    env("response_item", {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "My mistake — removed the emoji."}]}, 7),
]
with open(os.path.join(xdir, "rollout-2026-11-20T18-00-00-x1.jsonl"), "w") as f:
    for r in x1: f.write(json.dumps(r) + "\n")
# legacy flat format, other repo
x2 = [{"id": "x2", "timestamp": ts(50), "instructions": ""},
      {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "fix the bug"}]},
      {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Fixed."}]},
      {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "wrong file, it's in utils.py"}]}]
with open(os.path.join(xdir, "rollout-2026-11-20T19-00-00-x2.jsonl"), "w") as f:
    for r in x2: f.write(json.dumps(r) + "\n")
print("fixtures at", H)
