#!/usr/bin/env python3
"""
Change ledger for agent-backprop: backups, tiers, online metric checks, rollback.

Every edit the skill makes to a SKILL.md / CLAUDE.md / AGENTS.md goes through:
  begin   --id ID --files F [F ...]          snapshot files before editing
  (edit the files)
  finish  --id ID --tier auto|notify|approve --scope-kind skill|repo|user
          --scope NAME [--lessons L1,L2] [--signals correction,...] --summary TEXT
          approve-tier edits are reverted to the snapshot and kept as a proposal
  accept  --id ID                            apply a proposal (after the user says yes)
  reject  --id ID                            drop a proposal
  check   [--min-sessions 5]                 compare post-change metrics vs baseline
  rollback --id ID                           restore the snapshot (refuses if file drifted)
  list    [--status applied|proposed|...]

State lives in ~/.agent-backprop (override with --state-dir).
Metrics come from every <state>/runs/*/sessions.jsonl written by extract.py.
"""
import argparse
import datetime as dt
import difflib
import glob
import hashlib
import json
import os
import shutil
import sys

HOME = os.path.expanduser("~")


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha(path):
    if not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def load(state):
    p = os.path.join(state, "ledger.json")
    if os.path.exists(p):
        with open(p) as fh:
            return json.load(fh)
    return {"changes": {}}


def save(state, led):
    p = os.path.join(state, "ledger.json")
    tmp = p + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(led, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, p)


def safe_name(path):
    return hashlib.sha1(os.path.abspath(path).encode()).hexdigest()[:12] + "_" + os.path.basename(path)


def all_sessions(state):
    rows = {}
    for p in sorted(glob.glob(os.path.join(state, "runs", "*", "sessions.jsonl"))):
        with open(p) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                prev = rows.get(r["session"])
                if prev is None or (r.get("end") or "") >= (prev.get("end") or ""):
                    rows[r["session"]] = r
    return list(rows.values())


def in_scope(row, kind, scope):
    if kind == "skill":
        return scope in row.get("skills_used", [])
    if kind == "repo":
        return row.get("repo") == scope or (row.get("repo_root") or "") == scope
    return True  # user-level instruction change affects everything


SIGNAL_KEYS = {
    "correction": ["corrections", "interrupts"],
    "correction_after_done_claim": ["false_done_candidates"],
    "interrupt": ["interrupts"],
    "avoidable_question": ["avoidable_question_candidates"],
    "self_correction": ["self_corrections"],
    "retry_loop": ["retry_loops", "tool_errors"],
}


def rate(rows, keys):
    if not rows:
        return None
    return round(sum(sum(r["metrics"].get(k, 0) for k in keys) for r in rows) / len(rows), 4)


def metric_keys(signals):
    keys = set()
    for s in signals or ["correction"]:
        keys.update(SIGNAL_KEYS.get(s, ["corrections", "interrupts"]))
    return sorted(keys)


def snapshot_metrics(state, kind, scope, signals, before_iso, days=45):
    cutoff = (dt.datetime.fromisoformat(before_iso) - dt.timedelta(days=days)).isoformat()
    rows = [r for r in all_sessions(state) if in_scope(r, kind, scope)
            and (r.get("start") or "") < before_iso and (r.get("start") or "") >= cutoff]
    keys = metric_keys(signals)
    return {"n": len(rows), "keys": keys, "rate": rate(rows, keys),
            "rework": rate(rows, ["corrections", "interrupts", "avoidable_question_candidates"])}


def cmd_begin(a, led):
    if a.id in led["changes"] and led["changes"][a.id]["status"] != "editing":
        sys.exit(f"change {a.id} already exists with status {led['changes'][a.id]['status']}")
    bdir = os.path.join(a.state_dir, "backups", a.id, "before")
    os.makedirs(bdir, exist_ok=True)
    files = []
    for f in a.files:
        f = os.path.abspath(os.path.expanduser(f))
        dst = os.path.join(bdir, safe_name(f))
        existed = os.path.exists(f)
        if existed:
            shutil.copy2(f, dst)
        files.append({"path": f, "backup": dst if existed else None, "before_sha": sha(f)})
    led["changes"][a.id] = {"id": a.id, "status": "editing", "began": now(), "files": files}
    print(json.dumps({"ok": True, "id": a.id, "files": [x["path"] for x in files]}))


def cmd_finish(a, led):
    ch = led["changes"].get(a.id)
    if not ch or ch["status"] != "editing":
        sys.exit(f"no change {a.id} in editing state; run begin first")
    adir = os.path.join(a.state_dir, "backups", a.id, "after")
    os.makedirs(adir, exist_ok=True)
    diffs = []
    for f in ch["files"]:
        p = f["path"]
        after = os.path.join(adir, safe_name(p))
        if os.path.exists(p):
            shutil.copy2(p, after)
        f["after"] = after if os.path.exists(p) else None
        f["after_sha"] = sha(p)
        old = open(f["backup"], encoding="utf-8", errors="replace").read().splitlines(True) if f["backup"] else []
        new = open(p, encoding="utf-8", errors="replace").read().splitlines(True) if os.path.exists(p) else []
        diffs.append("".join(difflib.unified_diff(old, new, fromfile=p + " (before)", tofile=p + " (after)")))
    if not any(d.strip() for d in diffs):
        ch["status"] = "noop"
        print(json.dumps({"ok": True, "id": a.id, "status": "noop"}))
        return
    ch.update({
        "tier": a.tier, "scope_kind": a.scope_kind, "scope": a.scope,
        "lessons": [x for x in (a.lessons or "").split(",") if x],
        "signals": [x for x in (a.signals or "").split(",") if x],
        "summary": a.summary, "finished": now(), "diff": "\n".join(diffs),
    })
    ch["baseline"] = snapshot_metrics(a.state_dir, a.scope_kind, a.scope, ch["signals"], ch["finished"])
    if a.tier == "approve":
        for f in ch["files"]:  # revert working copy; keep as proposal
            restore(f, "backup")
        ch["status"] = "proposed"
    else:
        ch["status"] = "applied"
        ch["applied_at"] = ch["finished"]
    with open(os.path.join(a.state_dir, "backups", a.id, "change.diff"), "w") as fh:
        fh.write(ch["diff"])
    print(json.dumps({"ok": True, "id": a.id, "status": ch["status"], "baseline": ch["baseline"]}))


def restore(f, which):
    src = f.get(which)
    if src and os.path.exists(src):
        os.makedirs(os.path.dirname(f["path"]), exist_ok=True)
        shutil.copy2(src, f["path"])
    elif which == "backup" and f.get("backup") is None and os.path.exists(f["path"]):
        os.remove(f["path"])  # file did not exist before the change
        d = os.path.dirname(f["path"])
        try:
            if not os.listdir(d):
                os.rmdir(d)  # drop the now-empty skill folder
        except OSError:
            pass


def cmd_accept(a, led):
    ch = led["changes"].get(a.id)
    if not ch or ch["status"] != "proposed":
        sys.exit(f"{a.id} is not a pending proposal")
    for f in ch["files"]:
        if sha(f["path"]) != f["before_sha"]:
            sys.exit(f"{f['path']} changed since the proposal was made; regenerate the proposal")
    for f in ch["files"]:
        restore(f, "after")
    ch["status"] = "applied"
    ch["applied_at"] = now()
    ch["accepted_by_user"] = True
    print(json.dumps({"ok": True, "id": a.id, "status": "applied"}))


def cmd_reject(a, led):
    ch = led["changes"].get(a.id)
    if not ch or ch["status"] != "proposed":
        sys.exit(f"{a.id} is not a pending proposal")
    ch["status"] = "rejected"
    ch["rejected_at"] = now()
    print(json.dumps({"ok": True, "id": a.id, "status": "rejected"}))


def cmd_rollback(a, led):
    ch = led["changes"].get(a.id)
    if not ch or ch["status"] not in ("applied", "confirmed", "kept_inconclusive"):
        sys.exit(f"{a.id} is not an applied change")
    drift = [f["path"] for f in ch["files"] if sha(f["path"]) != f.get("after_sha")]
    if drift and not a.force:
        print(json.dumps({"ok": False, "id": a.id, "error": "files changed after this edit (later edits stacked on top)",
                          "drifted": drift, "hint": "revert the specific lines from backups/<id>/change.diff by hand, "
                                                    "or pass --force to restore the full snapshot"}))
        sys.exit(2)
    for f in ch["files"]:
        restore(f, "backup")
    ch["status"] = "rolled_back"
    ch["rolled_back_at"] = now()
    ch["rollback_reason"] = a.reason
    print(json.dumps({"ok": True, "id": a.id, "status": "rolled_back"}))


def cmd_check(a, led):
    sessions = all_sessions(a.state_dir)
    out = []
    for ch in led["changes"].values():
        if ch["status"] != "applied":
            continue
        start = ch["applied_at"]
        rows = [r for r in sessions if in_scope(r, ch["scope_kind"], ch["scope"]) and (r.get("start") or "") > start]
        keys = ch["baseline"]["keys"]
        after = rate(rows, keys)
        base = ch["baseline"]["rate"]
        n = len(rows)
        verdict = "watching"
        if n >= a.min_sessions:
            if base is None or ch["baseline"]["n"] < a.min_sessions:
                verdict = "keep_no_baseline" if n >= 3 * a.min_sessions else "watching"
            elif after > base * (1 + a.tolerance) + 0.05:
                verdict = "rollback_suggested"
            elif after <= base:
                verdict = "confirmed"
            elif n >= 3 * a.min_sessions:
                verdict = "kept_inconclusive"
        rec = {"id": ch["id"], "scope": f"{ch['scope_kind']}:{ch['scope']}", "tier": ch["tier"],
               "summary": ch.get("summary"), "n_after": n, "baseline_n": ch["baseline"]["n"],
               "baseline_rate": base, "after_rate": after, "keys": keys, "verdict": verdict}
        if a.apply:
            if verdict in ("confirmed", "kept_inconclusive", "keep_no_baseline"):
                ch["status"] = "confirmed" if verdict == "confirmed" else "kept_inconclusive"
                ch["checked_at"] = now()
            elif verdict == "rollback_suggested" and ch["tier"] in ("auto", "notify"):
                drift = [f["path"] for f in ch["files"] if sha(f["path"]) != f.get("after_sha")]
                if drift:
                    rec["auto_rollback"] = "skipped: files drifted; revert by hand"
                else:
                    for f in ch["files"]:
                        restore(f, "backup")
                    ch["status"] = "rolled_back"
                    ch["rolled_back_at"] = now()
                    ch["rollback_reason"] = f"metric regression {base} -> {after} over {n} sessions"
                    rec["auto_rollback"] = "done"
        out.append(rec)
    print(json.dumps({"checked": len(out), "results": out}, indent=1, ensure_ascii=False))


def cmd_list(a, led):
    rows = [{k: ch.get(k) for k in ("id", "status", "tier", "scope_kind", "scope", "summary", "finished", "applied_at")}
            for ch in led["changes"].values() if not a.status or ch["status"] == a.status]
    if a.with_diff:
        for r in rows:
            r["diff"] = led["changes"][r["id"]].get("diff", "")
    print(json.dumps(rows, indent=1, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state-dir", default=os.path.join(HOME, ".agent-backprop"))
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("begin"); p.add_argument("--id", required=True); p.add_argument("--files", nargs="+", required=True)
    p = sp.add_parser("finish"); p.add_argument("--id", required=True)
    p.add_argument("--tier", choices=["auto", "notify", "approve"], required=True)
    p.add_argument("--scope-kind", choices=["skill", "repo", "user"], required=True)
    p.add_argument("--scope", required=True, help="skill name, repo key, or 'user'")
    p.add_argument("--lessons", default=""); p.add_argument("--signals", default=""); p.add_argument("--summary", default="")
    p = sp.add_parser("accept"); p.add_argument("--id", required=True)
    p = sp.add_parser("reject"); p.add_argument("--id", required=True)
    p = sp.add_parser("rollback"); p.add_argument("--id", required=True); p.add_argument("--reason", default="manual")
    p.add_argument("--force", action="store_true")
    p = sp.add_parser("check"); p.add_argument("--min-sessions", type=int, default=5)
    p.add_argument("--tolerance", type=float, default=0.25); p.add_argument("--apply", action="store_true",
                                                                           help="mark confirmed / auto-rollback regressions")
    p = sp.add_parser("list"); p.add_argument("--status"); p.add_argument("--with-diff", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.state_dir, exist_ok=True)
    led = load(a.state_dir)
    {"begin": cmd_begin, "finish": cmd_finish, "accept": cmd_accept, "reject": cmd_reject,
     "rollback": cmd_rollback, "check": cmd_check, "list": cmd_list}[a.cmd](a, led)
    if a.cmd != "list":
        save(a.state_dir, led)


if __name__ == "__main__":
    main()
