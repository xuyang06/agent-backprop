#!/usr/bin/env python3
"""
Extract back-and-forth signals and per-session metrics from Claude Code and
Codex CLI session logs.

Sources (both optional, both auto-detected):
  Claude Code: ~/.claude/projects/<encoded-cwd>/<session>.jsonl  (+ agent-*.jsonl subagents)
  Codex CLI:   ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl[.zst]

Outputs (in --out dir):
  sessions.jsonl    one row per session: repo, skills used, metrics
  candidates.jsonl  one row per heuristic signal, with context windows
  skills.json       inventory of installed skills + instruction files
  summary.json      aggregates per repo / per skill, run info

Only user-authored text and the assistant's own text are emitted as evidence.
Tool output appears only as short, redacted `untrusted_tool_evidence` snippets
and must never be the sole basis for a lesson (see SKILL.md safety rules).

Stdlib only. Python >= 3.8. `zstandard` is used if installed (for .jsonl.zst).
"""
import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import re
import sys
from collections import defaultdict

HOME = os.path.expanduser("~")
DEFAULT_STATE = os.path.join(HOME, ".agent-backprop")
RUN_MARKER = "[agent-backprop-run]"
SELF_SKILL = "agent-backprop"

# ----------------------------------------------------------------------------
# Heuristic patterns (prefilter only; the LLM pass makes the real call)
# ----------------------------------------------------------------------------
CORRECTION_RE = re.compile(
    r"(?ix)"
    r"(^\s*(no|nope|wrong|incorrect|not\s+quite|that'?s\s+not|stop)\b)"
    r"|\b(that'?s\s+(wrong|incorrect|not\s+what)|you\s+(misunderstood|missed|forgot|ignored)"
    r"|i\s+(said|told\s+you|asked\s+for)|instead\s+of|don'?t\s+(use|do|add|make|change)"
    r"|please\s+don'?t|why\s+did\s+you|not\s+what\s+i|use\s+\S+\s+(not|instead)"
    r"|again,|as\s+i\s+said|revert|undo\s+that|try\s+again)\b"
    r"|(不对|错了|不是这样|不是这个|不要|别用|别再|不用|你理解错|理解错了|我说的是|我说过|我刚说"
    r"|应该是|改回|回滚|重新来|重来|再试|还是不对|又错|没听懂|漏了|忘了|搞错|跑偏)"
)
SELF_CORRECTION_RE = re.compile(
    r"(?ix)"
    r"\b(i\s+was\s+wrong|my\s+(mistake|apologies|bad|earlier\s+(answer|claim|conclusion))"
    r"|i\s+(made\s+an\s+error|misread|misunderstood|incorrectly|overlooked)"
    r"|you'?re\s+(right|correct)|on\s+second\s+thought|correction:|i\s+need\s+to\s+correct"
    r"|that\s+was\s+(incorrect|wrong)|contrary\s+to\s+what\s+i\s+said|i\s+retract)\b"
    r"|(我之前(说|的结论|的判断)?(有误|错了|不对)|更正|纠正一下|我搞错了|我理解错了|你说得对|您说得对|是我的错|我刚才(说|的)?错)"
)
DONE_CLAIM_RE = re.compile(
    r"(?ix)\b(all\s+(tests\s+)?pass(ing|ed)?|tests?\s+(now\s+)?pass(ing|ed)?|(it'?s|is\s+now|now)\s+(fixed|working)"
    r"|successfully\s+(implemented|fixed|completed|installed)|should\s+(now\s+)?work)\b|(^|\n)\s*done\b"
    r"|(已(经)?(完成|修复|搞定|解决)|全部通过|测试通过|现在可以了|应该可以了)"
)
AFFIRM_SHORT_RE = re.compile(
    r"(?ix)^\s*(y|yes|yeah|yep|ok|okay|sure|go(\s+ahead)?|proceed|do\s+it|sounds\s+good|lgtm|default|either"
    r"|whatever|your\s+call|you\s+decide|both|all|1|2|3|a|b"
    r"|好|好的|可以|行|对|是|嗯|继续|开始|都行|随便|你决定|默认|按你说的|没问题|就这样)\W*$"
)
QUESTION_END_RE = re.compile(r"[?？]\s*(\*\*)?\s*$")
INTERRUPT_RE = re.compile(r"\[Request interrupted by user")

SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"\b(sk|rk|pk)-(ant-|proj-|live-|test-)?[A-Za-z0-9_\-]{16,}"), "[REDACTED_KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED_GH_TOKEN]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), "[REDACTED_SLACK_TOKEN]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), "[REDACTED_GCP_KEY]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"), "[REDACTED_JWT]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{16,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret)(\s*[:=]\s*)(['\"]?)[^\s'\"]{6,}"),
     r"\1\2\3[REDACTED]"),
]


def redact(text):
    if not text:
        return text
    for pat, rep in SECRET_PATTERNS:
        text = pat.sub(rep, text)
    return text


def clip(text, n):
    if text is None:
        return ""
    text = text.strip()
    if len(text) <= n:
        return text
    half = n // 2
    return text[:half] + f"\n…[{len(text) - n} chars clipped]…\n" + text[-half:]


def parse_ts(s):
    if not s:
        return None
    try:
        if isinstance(s, (int, float)):
            return dt.datetime.fromtimestamp(s / (1000 if s > 1e12 else 1), tz=dt.timezone.utc)
        s = s.replace("Z", "+00:00")
        t = dt.datetime.fromisoformat(s)
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return t
    except Exception:
        return None


def parse_since(s):
    if not s or s == "all":
        return None
    m = re.fullmatch(r"(\d+)([hd])", s)
    now = dt.datetime.now(dt.timezone.utc)
    if m:
        n, u = int(m.group(1)), m.group(2)
        return now - (dt.timedelta(hours=n) if u == "h" else dt.timedelta(days=n))
    t = parse_ts(s)
    if t is None:
        sys.exit(f"bad --since value: {s}")
    return t


# ----------------------------------------------------------------------------
# Repo resolution
# ----------------------------------------------------------------------------
_repo_cache = {}


def resolve_repo(cwd, remote_hint=None):
    """Map a cwd to a stable repo key: git remote URL if found, else git toplevel, else cwd."""
    if not cwd:
        return {"key": remote_hint or "(unknown)", "root": None, "remote": remote_hint}
    if cwd in _repo_cache:
        r = dict(_repo_cache[cwd])
        if remote_hint and not r.get("remote"):
            r["remote"] = remote_hint
            r["key"] = normalize_remote(remote_hint)
        return r
    root, gitdir = None, None
    p = os.path.abspath(cwd)
    while True:
        g = os.path.join(p, ".git")
        if os.path.isdir(g):
            root, gitdir = p, g
            break
        if os.path.isfile(g):  # worktree or submodule: "gitdir: <path>"
            try:
                with open(g) as fh:
                    line = fh.read().strip()
                if line.startswith("gitdir:"):
                    wt = line.split(":", 1)[1].strip()
                    wt = wt if os.path.isabs(wt) else os.path.normpath(os.path.join(p, wt))
                    m = re.search(r"^(.*?/\.git)(/worktrees/.*)?$", wt)
                    gitdir = m.group(1) if m else wt
                    root = os.path.dirname(gitdir) if gitdir.endswith("/.git") else p
            except OSError:
                root = p
            break
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    remote = remote_hint
    if gitdir and not remote:
        try:
            with open(os.path.join(gitdir, "config")) as fh:
                cfg = fh.read()
            m = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', cfg, re.S) or \
                re.search(r'\[remote "[^"]+"\][^\[]*?url\s*=\s*(\S+)', cfg, re.S)
            if m:
                remote = m.group(1)
        except OSError:
            pass
    key = normalize_remote(remote) if remote else (root or os.path.abspath(cwd))
    r = {"key": key, "root": root or (os.path.abspath(cwd) if os.path.isdir(cwd) else None), "remote": remote}
    _repo_cache[cwd] = r
    return dict(r)


def normalize_remote(url):
    u = url.strip()
    u = re.sub(r"^(git@|ssh://git@)([^:/]+)[:/]", r"\2/", u)
    u = re.sub(r"^https?://([^@/]+@)?", "", u)
    u = re.sub(r"\.git$", "", u)
    return u.lower()


def repo_matches(repo, want):
    if not want:
        return True
    w = os.path.abspath(os.path.expanduser(want)) if (want.startswith(("/", "~", "."))) else want
    if os.path.isdir(w):
        target = resolve_repo(w)
        return repo["key"] == target["key"] or (repo.get("root") and target.get("root") and repo["root"] == target["root"])
    return want.lower() in repo["key"].lower()


# ----------------------------------------------------------------------------
# Normalized session model
#   turn = {role: user|assistant|tool, text, ts, error(bool, tool only),
#           cmd (tool only, model-authored), skill_hits: [names]}
# ----------------------------------------------------------------------------

def _text_from_content(content, want_types=("text",)):
    if isinstance(content, str):
        return content
    out = []
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") in want_types and isinstance(c.get("text"), str):
                out.append(c["text"])
    return "\n".join(out)


def _stringify_tool_content(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
    return json.dumps(c)[:2000] if c is not None else ""


CLAUDE_NOISE_PREFIXES = ("<command-message>", "<local-command-stdout>", "<local-command-stderr>",
                         "Caveat: The messages below", "<system-reminder>", "<bash-stdout>", "<bash-stderr>",
                         "This session is being continued from a previous conversation")


def read_jsonl(path):
    opener = None
    if path.endswith(".zst"):
        try:
            import zstandard  # type: ignore
            import io
            fh = open(path, "rb")
            stream = zstandard.ZstdDecompressor().stream_reader(fh)
            opener = io.TextIOWrapper(stream, encoding="utf-8", errors="replace")
        except ImportError:
            print(f"[warn] skipping {path}: pip install zstandard to read .zst rollouts", file=sys.stderr)
            return
    else:
        opener = open(path, encoding="utf-8", errors="replace")
    with opener as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def parse_claude(path):
    turns, cwd, sid, branch = [], None, None, None
    pending_tools = {}  # tool_use_id -> cmd summary
    for rec in read_jsonl(path):
        t = rec.get("type")
        cwd = cwd or rec.get("cwd")
        sid = sid or rec.get("sessionId")
        branch = branch or rec.get("gitBranch")
        if t not in ("user", "assistant"):
            continue
        if rec.get("isCompactSummary") or rec.get("isMeta"):
            continue
        msg = rec.get("message") or {}
        content = msg.get("content")
        ts = rec.get("timestamp")
        if t == "assistant":
            text = _text_from_content(content)
            skills = []
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        inp = c.get("input") or {}
                        name = c.get("name", "")
                        if name == "Skill":
                            s = inp.get("skill") or inp.get("command") or inp.get("name")
                            if s:
                                skills.append(str(s).lstrip("/"))
                        cmd = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("url") or ""
                        if isinstance(cmd, list):
                            cmd = " ".join(map(str, cmd))
                        if "SKILL.md" in str(cmd) or "SKILL.md" in json.dumps(inp)[:500]:
                            m = re.search(r"([A-Za-z0-9_.\-]+)/SKILL\.md", json.dumps(inp))
                            if m:
                                skills.append(m.group(1))
                        pending_tools[c.get("id")] = f"{name}: {str(cmd)[:300]}"
            if text.strip() or skills:
                turns.append({"role": "assistant", "text": text, "ts": ts, "skill_hits": skills})
        else:  # user
            if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        err = bool(c.get("is_error"))
                        out = _stringify_tool_content(c.get("content"))
                        if not err and re.search(r"(?i)^(error|traceback|exit code [1-9])", out.strip()[:200]):
                            err = True
                        turns.append({"role": "tool", "error": err, "text": out[:600], "ts": ts,
                                      "cmd": pending_tools.get(c.get("tool_use_id"), "")})
                continue
            text = _text_from_content(content)
            if not text.strip():
                continue
            if INTERRUPT_RE.search(text):
                turns.append({"role": "interrupt", "text": text, "ts": ts})
                continue
            skills = []
            m = re.search(r"<command-name>/?([^<\s]+)</command-name>", text)
            if m:
                skills.append(m.group(1))
                args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
                text = f"/{m.group(1)} " + (args.group(1) if args else "")
            elif text.lstrip().startswith(CLAUDE_NOISE_PREFIXES):
                continue
            turns.append({"role": "user", "text": text, "ts": ts, "skill_hits": skills})
    return {"source": "claude", "session_id": sid or os.path.basename(path)[:-6], "cwd": cwd,
            "branch": branch, "remote": None, "turns": turns,
            "is_subagent": os.path.basename(path).startswith("agent-")}


CODEX_INJECTED_PREFIXES = ("<environment_context>", "<user_instructions>", "# AGENTS.md", "<INSTRUCTIONS>",
                           "<permissions", "<turn_aborted>")


def parse_codex(path):
    turns, cwd, sid, remote, branch = [], None, None, None, None
    ev_users, ri_users = [], []
    have_fco = False
    exec_end = []
    calls = {}
    for rec in read_jsonl(path):
        rtype = rec.get("type")
        payload = rec.get("payload")
        ts = rec.get("timestamp")
        if payload is None:  # legacy flat layout
            payload, rtype = rec, ("response_item" if rec.get("type") in
                                   ("message", "function_call", "function_call_output", "reasoning",
                                    "local_shell_call", "custom_tool_call", "custom_tool_call_output") else rec.get("type"))
            if "instructions" in rec and "id" in rec and not rec.get("type"):
                sid = sid or rec.get("id")
                continue
        if not isinstance(payload, dict):
            continue
        if rtype == "session_meta":
            sid = sid or payload.get("id") or payload.get("session_id")
            cwd = cwd or payload.get("cwd")
            g = payload.get("git") or {}
            remote = remote or g.get("repository_url")
            branch = branch or g.get("branch")
            continue
        if rtype == "turn_context":
            cwd = cwd or payload.get("cwd")
            continue
        if rtype == "event_msg":
            pt = payload.get("type")
            if pt == "user_message" and isinstance(payload.get("message"), str):
                ev_users.append({"role": "user", "text": payload["message"], "ts": ts, "skill_hits": []})
            elif pt in ("turn_aborted",):
                turns.append({"role": "interrupt", "text": "turn aborted", "ts": ts})
            elif pt == "exec_command_end":
                code = payload.get("exit_code")
                cmd = payload.get("command")
                if isinstance(cmd, list):
                    cmd = " ".join(map(str, cmd))
                exec_end.append({"role": "tool", "error": bool(code not in (0, None)), "ts": ts,
                                 "text": str(payload.get("stderr") or payload.get("aggregated_output") or "")[:600],
                                 "cmd": f"shell: {str(cmd)[:300]}"})
            continue
        if rtype != "response_item":
            continue
        it = payload.get("type")
        if it == "message":
            role = payload.get("role")
            if role == "user":
                text = _text_from_content(payload.get("content"), ("input_text", "text"))
                if text.strip() and not text.lstrip().startswith(CODEX_INJECTED_PREFIXES):
                    ri_users.append({"role": "user", "text": text, "ts": ts, "skill_hits": []})
                    turns.append({"role": "_user_slot", "ts": ts})
            elif role == "assistant":
                text = _text_from_content(payload.get("content"), ("output_text", "text"))
                if text.strip():
                    turns.append({"role": "assistant", "text": text, "ts": ts, "skill_hits": []})
        elif it in ("function_call", "custom_tool_call", "local_shell_call"):
            args = payload.get("arguments") or payload.get("input") or payload.get("action") or ""
            if not isinstance(args, str):
                args = json.dumps(args)
            skills = re.findall(r"([A-Za-z0-9_.\-]+)/SKILL\.md", args)
            calls[payload.get("call_id")] = f"{payload.get('name', it)}: {args[:300]}"
            if skills:
                turns.append({"role": "assistant", "text": "", "ts": ts, "skill_hits": skills})
        elif it in ("function_call_output", "custom_tool_call_output"):
            have_fco = True
            out = payload.get("output")
            err, text = False, ""
            if isinstance(out, str):
                text = out
                try:
                    j = json.loads(out)
                    if isinstance(j, dict):
                        code = (j.get("metadata") or {}).get("exit_code")
                        err = code not in (0, None)
                        text = str(j.get("output", ""))
                except (json.JSONDecodeError, TypeError):
                    m = re.search(r"(?i)exit(ed with)? code:?\s*(-?\d+)", out[:300])
                    err = bool(m and m.group(2) != "0")
            elif isinstance(out, dict):
                err = (out.get("metadata") or {}).get("exit_code") not in (0, None)
                text = str(out.get("output", ""))
            turns.append({"role": "tool", "error": err, "text": text[:600], "ts": ts,
                          "cmd": calls.get(payload.get("call_id"), "")})
    # Prefer event_msg user messages (exactly what the human typed); fall back to response items.
    users = ev_users if ev_users else ri_users
    merged, ui = [], 0
    if ev_users and ri_users and len(ev_users) == len(ri_users):
        for t in turns:
            if t["role"] == "_user_slot":
                if ui < len(users):
                    merged.append(users[ui]); ui += 1
            else:
                merged.append(t)
    elif ri_users and not ev_users:
        for t in turns:
            if t["role"] == "_user_slot":
                merged.append(users[ui]); ui += 1
            else:
                merged.append(t)
    else:  # interleave by timestamp
        merged = sorted([t for t in turns if t["role"] != "_user_slot"] + users,
                        key=lambda t: (parse_ts(t.get("ts")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)))
    if not have_fco and exec_end:
        merged = sorted(merged + exec_end,
                        key=lambda t: (parse_ts(t.get("ts")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)))
    # $skill mentions in user text
    for t in merged:
        if t["role"] == "user":
            t["skill_hits"] = t.get("skill_hits", []) + re.findall(r"(?<![\w$])\$([a-z0-9][a-z0-9_\-]{1,63})\b", t["text"])
    sid = sid or re.sub(r"^rollout-[\dT\-]+-", "", os.path.basename(path)).split(".")[0]
    return {"source": "codex", "session_id": sid, "cwd": cwd, "branch": branch, "remote": remote,
            "turns": merged, "is_subagent": False}


# ----------------------------------------------------------------------------
# Skill inventory
# ----------------------------------------------------------------------------

def skill_roots(extra_repo_roots):
    roots = [("user", os.path.join(HOME, ".claude", "skills")),
             ("user", os.path.join(HOME, ".agents", "skills")),
             ("user", os.path.join(HOME, ".codex", "skills"))]
    for r in extra_repo_roots:
        for sub in (".claude/skills", ".agents/skills", ".codex/skills"):
            roots.append(("repo:" + r, os.path.join(r, sub)))
    return roots


def inventory(repo_roots):
    skills, seen = [], set()
    for scope, root in skill_roots(repo_roots):
        for p in glob.glob(os.path.join(root, "*", "SKILL.md")):
            real = os.path.realpath(p)
            if real in seen:
                continue
            seen.add(real)
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    body = fh.read()
            except OSError:
                continue
            fm = re.match(r"^---\s*\n(.*?)\n---", body, re.S)
            name, desc = os.path.basename(os.path.dirname(p)), ""
            if fm:
                m = re.search(r"^name:\s*(.+)$", fm.group(1), re.M)
                name = m.group(1).strip().strip("'\"") if m else name
                m = re.search(r"^description:\s*(.+?)(?=^\w[\w-]*:|\Z)", fm.group(1), re.M | re.S)
                desc = re.sub(r"\s+", " ", m.group(1)).strip().strip("'\"|>") if m else ""
            skills.append({"name": name, "scope": scope, "path": p, "realpath": real,
                           "writable": os.access(p, os.W_OK) and "/plugins/" not in real,
                           "lines": body.count("\n") + 1, "description": desc[:600],
                           "learned_entries": len(re.findall(r"<!--\s*sr:id=", body))})
    instr = []
    for p in [os.path.join(HOME, ".claude", "CLAUDE.md"), os.path.join(HOME, ".codex", "AGENTS.md")] + \
             [os.path.join(r, f) for r in repo_roots for f in ("CLAUDE.md", "AGENTS.md", ".claude/CLAUDE.md")]:
        if os.path.isfile(p):
            instr.append({"path": p, "scope": "user" if p.startswith(os.path.join(HOME, ".c")) else "repo",
                          "lines": sum(1 for _ in open(p, encoding="utf-8", errors="replace"))})
    return {"skills": skills, "instruction_files": instr}


# ----------------------------------------------------------------------------
# Signal detection + metrics
# ----------------------------------------------------------------------------

def analyze(sess, start_index=0, since=None):
    turns = sess["turns"]
    users = [i for i, t in enumerate(turns) if t["role"] == "user"]
    first_user = turns[users[0]]["text"] if users else ""
    skills_used = sorted({s for t in turns for s in t.get("skill_hits", []) if s})
    m = {"user_turns": len(users), "followup_user_turns": max(0, len(users) - 1),
         "corrections": 0, "interrupts": 0, "tool_calls": 0, "tool_errors": 0,
         "questions_asked": 0, "avoidable_question_candidates": 0, "self_corrections": 0,
         "retry_loops": 0, "false_done_candidates": 0}
    cands = []

    def prev(i, role):
        for j in range(i - 1, -1, -1):
            if turns[j]["role"] == role and turns[j].get("text", "").strip():
                return j
        return None

    def nxt(i, role):
        for j in range(i + 1, len(turns)):
            if turns[j]["role"] == role and turns[j].get("text", "").strip():
                return j
        return None

    def ctx(i, kind, evidence_role, extra=None):
        pa, na = prev(i, "assistant"), nxt(i, "assistant")
        c = {
            "kind": kind, "turn_index": i, "ts": turns[i].get("ts"),
            "evidence_role": evidence_role,
            "evidence": redact(clip(turns[i].get("text", ""), 2000)),
            "prev_assistant": redact(clip(turns[pa]["text"], 1500)) if pa is not None else "",
            "next_assistant": redact(clip(turns[na]["text"], 800)) if na is not None else "",
            "first_user_prompt": redact(clip(first_user, 1500)),
            "skills_active": sorted({s for t in turns[:i + 1] for s in t.get("skill_hits", []) if s}),
        }
        if extra:
            c.update(extra)
        return c

    def in_window(i):
        if i < start_index:
            return False
        if since is not None:
            t = parse_ts(turns[i].get("ts"))
            if t is not None and t < since:
                return False
        return True

    last_done_claim = None
    err_streak, streak_cmds, streak_errs = 0, [], []
    for i, t in enumerate(turns):
        r = t["role"]
        if r == "tool":
            m["tool_calls"] += 1
            if t.get("error"):
                m["tool_errors"] += 1
                err_streak += 1
                streak_cmds.append(t.get("cmd", ""))
                streak_errs.append(t.get("text", ""))
            else:
                if err_streak >= 2:
                    m["retry_loops"] += 1
                    if in_window(i):
                        cands.append(ctx(i, "retry_loop", "assistant", {
                            "failed_commands": [redact(c[:300]) for c in streak_cmds[-4:]],
                            "succeeded_command": redact(t.get("cmd", "")[:300]),
                            "untrusted_tool_evidence": [redact(clip(e, 300)) for e in streak_errs[-2:]],
                            "evidence": redact(t.get("cmd", "")[:300])}))
                err_streak, streak_cmds, streak_errs = 0, [], []
            continue
        if r == "interrupt":
            m["interrupts"] += 1
            nu = nxt(i, "user")
            # if the follow-up message is itself a correction, that candidate carries the evidence
            if in_window(i) and not (nu is not None and CORRECTION_RE.search(turns[nu]["text"][:600])):
                c = ctx(i, "interrupt", "user")
                if nu is not None:
                    c["evidence"] = redact(clip(turns[nu]["text"], 2000))
                cands.append(c)
            continue
        text = t.get("text", "")
        if r == "assistant" and text.strip():
            if SELF_CORRECTION_RE.search(text):
                m["self_corrections"] += 1
                if in_window(i):
                    cands.append(ctx(i, "self_correction", "assistant"))
            if DONE_CLAIM_RE.search(text):
                last_done_claim = i
            if QUESTION_END_RE.search(text.strip()[-200:]):
                m["questions_asked"] += 1
                nu = nxt(i, "user")
                if nu is not None:
                    reply = turns[nu]["text"].strip()
                    if len(reply) <= 40 and AFFIRM_SHORT_RE.match(reply):
                        m["avoidable_question_candidates"] += 1
                        if in_window(nu):
                            c = ctx(nu, "avoidable_question", "user")
                            c["question"] = redact(clip(text, 1200))
                            cands.append(c)
        elif r == "user" and i != (users[0] if users else -1):
            if CORRECTION_RE.search(text[:600]):
                m["corrections"] += 1
                kind = "correction"
                if last_done_claim is not None and last_done_claim < i:
                    m["false_done_candidates"] += 1
                    kind = "correction_after_done_claim"
                if in_window(i):
                    cands.append(ctx(i, kind, "user"))
            last_done_claim = None
    m["rework_ratio"] = round(m["followup_user_turns"] and (m["corrections"] + m["interrupts"]) / m["followup_user_turns"], 3)
    return m, skills_used, cands, first_user


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def discover(claude_dir, codex_dir, since):
    files = []
    if claude_dir and os.path.isdir(claude_dir):
        files += [("claude", p) for p in glob.glob(os.path.join(claude_dir, "*", "*.jsonl"))]
    if codex_dir and os.path.isdir(codex_dir):
        files += [("codex", p) for p in glob.glob(os.path.join(codex_dir, "**", "rollout-*.jsonl*"), recursive=True)]
    if since is not None:
        cutoff = since.timestamp()
        files = [(s, p) for s, p in files if os.path.getmtime(p) >= cutoff]
    return sorted(files, key=lambda x: os.path.getmtime(x[1]))


def is_reflection_session(sess, first_user):
    if RUN_MARKER in (first_user or "")[:500]:
        return True
    head = (first_user or "")[:200]
    if re.search(rf"(^|\s)[/$]{SELF_SKILL}\b", head):
        return True
    return any(s == SELF_SKILL for t in sess["turns"][:3] for s in t.get("skill_hits", []))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["daily", "bootstrap"], default="daily",
                    help="daily: incremental since last run (state file). bootstrap: ignore state, full history.")
    ap.add_argument("--since", default=None, help="'all', '24h', '7d', or ISO date. Default: daily=state/36h, bootstrap=all")
    ap.add_argument("--repo", action="append", default=[],
                    help="Limit to repo(s): a local path or a substring of the remote URL. Repeatable.")
    ap.add_argument("--claude-dir", default=os.path.join(HOME, ".claude", "projects"))
    ap.add_argument("--codex-dir", default=os.path.join(os.environ.get("CODEX_HOME", os.path.join(HOME, ".codex")), "sessions"))
    ap.add_argument("--state-dir", default=DEFAULT_STATE)
    ap.add_argument("--out", default=None, help="Output dir (default: <state>/runs/<timestamp>)")
    ap.add_argument("--include-subagents", action="store_true", help="Also analyze Claude subagent transcripts")
    ap.add_argument("--no-state-update", action="store_true")
    a = ap.parse_args()

    os.makedirs(a.state_dir, exist_ok=True)
    state_path = os.path.join(a.state_dir, "state.json")
    state = {"files": {}, "last_run": None}
    if os.path.exists(state_path):
        with open(state_path) as fh:
            state = json.load(fh)
    use_state = a.mode == "daily"

    since_arg = a.since or ("all" if a.mode == "bootstrap" else None)
    if since_arg is None:
        since = parse_ts(state.get("last_run")) or (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=36))
    else:
        since = parse_since(since_arg)

    run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = a.out or os.path.join(a.state_dir, "runs", f"{run_id}-{a.mode}")
    os.makedirs(out, exist_ok=True)

    files = discover(a.claude_dir, a.codex_dir, since if a.mode == "daily" or since_arg != "all" else None)
    sess_rows, cand_rows = [], []
    repo_roots = set()
    skipped = defaultdict(int)
    for src, path in files:
        st = os.stat(path)
        prev_state = state["files"].get(path, {}) if use_state else {}
        if prev_state and prev_state.get("size") == st.st_size and prev_state.get("mtime") == st.st_mtime:
            skipped["unchanged"] += 1
            continue
        try:
            sess = parse_claude(path) if src == "claude" else parse_codex(path)
        except Exception as e:  # never let one bad file kill the run
            print(f"[warn] failed to parse {path}: {e}", file=sys.stderr)
            skipped["parse_error"] += 1
            continue
        if sess["is_subagent"] and not a.include_subagents:
            skipped["subagent"] += 1
            continue
        start = prev_state.get("turns", 0) if use_state else 0
        metrics, skills_used, cands, first_user = analyze(sess, start_index=start,
                                                          since=since if a.mode == "daily" else (since if since_arg != "all" else None))
        state["files"][path] = {"size": st.st_size, "mtime": st.st_mtime, "turns": len(sess["turns"])}
        if not sess["turns"] or metrics["user_turns"] == 0:
            skipped["empty"] += 1
            continue
        if is_reflection_session(sess, first_user):
            skipped["agent-backprop_run"] += 1
            continue
        repo = resolve_repo(sess["cwd"], sess.get("remote"))
        if a.repo and not any(repo_matches(repo, r) for r in a.repo):
            skipped["other_repo"] += 1
            continue
        if repo.get("root"):
            repo_roots.add(repo["root"])
        ts_all = [parse_ts(t.get("ts")) for t in sess["turns"] if t.get("ts")]
        ts_all = [x for x in ts_all if x]
        sref = f"{src}:{sess['session_id']}"
        sess_rows.append({
            "session": sref, "source": src, "path": path, "cwd": sess["cwd"], "branch": sess.get("branch"),
            "repo": repo["key"], "repo_root": repo.get("root"),
            "start": min(ts_all).isoformat() if ts_all else None, "end": max(ts_all).isoformat() if ts_all else None,
            "skills_used": skills_used, "first_user_prompt": redact(clip(first_user, 400)), "metrics": metrics,
        })
        for c in cands:
            c["session"] = sref
            c["repo"] = repo["key"]
            c["repo_root"] = repo.get("root")
            c["source"] = src
            c["id"] = "C-" + hashlib.sha1(f"{sref}#{c['turn_index']}#{c['kind']}".encode()).hexdigest()[:10]
            cand_rows.append(c)

    with open(os.path.join(out, "sessions.jsonl"), "w") as fh:
        for r in sess_rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(os.path.join(out, "candidates.jsonl"), "w") as fh:
        for r in cand_rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    inv = inventory(sorted(repo_roots))
    with open(os.path.join(out, "skills.json"), "w") as fh:
        json.dump(inv, fh, ensure_ascii=False, indent=1)

    # Aggregates
    def agg(rows):
        n = len(rows)
        if not n:
            return {"sessions": 0}
        keys = ["followup_user_turns", "corrections", "interrupts", "tool_errors", "avoidable_question_candidates",
                "self_corrections", "retry_loops", "false_done_candidates"]
        d = {"sessions": n}
        for k in keys:
            d["avg_" + k] = round(sum(r["metrics"][k] for r in rows) / n, 3)
        d["sessions_with_any_correction"] = sum(1 for r in rows if r["metrics"]["corrections"] + r["metrics"]["interrupts"] > 0)
        return d

    by_repo, by_skill = defaultdict(list), defaultdict(list)
    for r in sess_rows:
        by_repo[r["repo"]].append(r)
        for s in r["skills_used"]:
            by_skill[s].append(r)
    kinds = defaultdict(int)
    for c in cand_rows:
        kinds[c["kind"]] += 1
    summary = {
        "run_id": run_id, "mode": a.mode, "since": since.isoformat() if since else "all",
        "files_seen": len(files), "sessions_analyzed": len(sess_rows), "candidates": len(cand_rows),
        "candidates_by_kind": dict(kinds), "skipped": dict(skipped),
        "overall": agg(sess_rows),
        "by_repo": {k: agg(v) for k, v in sorted(by_repo.items(), key=lambda x: -len(x[1]))},
        "by_skill": {k: agg(v) for k, v in sorted(by_skill.items(), key=lambda x: -len(x[1]))},
        "out_dir": out,
    }
    with open(os.path.join(out, "summary.json"), "w") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)

    if use_state and not a.no_state_update:
        state["last_run"] = dt.datetime.now(dt.timezone.utc).isoformat()
        tmp = state_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, state_path)

    print(json.dumps({k: summary[k] for k in ("mode", "since", "files_seen", "sessions_analyzed", "candidates",
                                               "candidates_by_kind", "skipped", "out_dir")}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
