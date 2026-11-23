# Setup, scheduling, troubleshooting

## Install
```bash
./install.sh --repo ~/code/foo --repo ~/code/bar --schedule      # macOS launchd at 03:17 daily
```
What it does: copies the skill to `~/.claude/skills/agent-backprop`, symlinks it into `~/.agents/skills`
(and `~/.codex/skills` if `~/.codex` exists), sets `cleanupPeriodDays: 3650` in `~/.claude/settings.json`
(backup kept; never 0 — 0 stops transcripts from being written), writes `~/.agent-backprop/config.json`.

## Unattended runs
`scripts/run_daily.sh [daily|bootstrap]` runs the agent headless with a `[agent-backprop-run]` marker
(so its own session is excluded from future extraction), least-privilege tools, a lock, a log in
`~/.agent-backprop/logs/`, and a desktop notification only when the digest has pending decisions.
`AGENT_BACKPROP_AGENT=codex` switches to `codex exec`.

Verify on your versions once:
- Claude Code: `--allowedTools` rule syntax and `--add-dir` (run `claude --help`).
- Codex: whether `codex exec` reads the prompt from stdin with `-` (used by `replay_eval.py --runner "codex exec -"`),
  and which extra writable roots it needs to edit `~/.claude` / repos under `--full-auto`.

## State layout (`~/.agent-backprop/`)
| path | what |
|---|---|
| `state.json` | per-file size/mtime/turn count for incremental extraction |
| `runs/<ts>-<mode>/` | `sessions.jsonl`, `candidates.jsonl`, `skills.json`, `summary.json`, `triage.jsonl`, replay outputs |
| `lessons.json` | lesson registry (evidence accumulates across days) |
| `ledger.json`, `backups/<id>/` | every change: before/after snapshots, diff, tier, baseline metrics, status |
| `cases/<target>.jsonl` | replay cases; older ones become the regression set |
| `reports/` | digests |

## Useful commands
```bash
SR=~/.claude/skills/agent-backprop/scripts
python3 $SR/extract.py --mode bootstrap --repo . --out /tmp/sr     # dry look at signals, no state change
python3 $SR/ledger.py list --status proposed --with-diff
python3 $SR/ledger.py accept --id N-002
python3 $SR/ledger.py rollback --id L-1a2b3c4d --reason "didn't help"
python3 $SR/ledger.py check                                         # metrics only, no changes
```

## Coverage limits
- claude.ai / Claude desktop chat conversations are not on disk; only Claude Code (incl. desktop/IDE Claude Code
  sessions) and Codex CLI/IDE sessions are covered.
- Claude Code transcripts older than the retention window are already gone; bootstrap sees what's left.
- Compressed Codex rollouts (`.jsonl.zst`) need `pip install zstandard`.
- Transcripts can contain secrets; the extractor redacts common token formats before anything is sent to a model,
  but it is pattern-based. Add patterns to `SECRET_PATTERNS` in `extract.py` for your org's token formats.
