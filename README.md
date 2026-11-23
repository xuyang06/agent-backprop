# agent-backprop

Backpropagation for your coding agents: every correction, interrupt, and redo is an error signal, propagated back into skills and CLAUDE.md / AGENTS.md. Offline replay is the validation set; metric regressions trigger rollback.

A skill for Claude Code and Codex that mines your past sessions for back-and-forth — corrections, interrupts,
avoidable clarifying questions, reversed conclusions, retry loops — and turns the recurring ones into
improvements to existing skills, `CLAUDE.md` / `AGENTS.md`, or new skills.

Goal: fewer turns per accepted result, with as little human review as possible. Changes are verified by
offline replay, applied in tiers (auto / notify / approve), and rolled back automatically when post-change
metrics get worse.

## Install

```bash
./install.sh --repo ~/code/<repo> --schedule
```

- copies the skill to `~/.claude/skills/agent-backprop` and symlinks it into `~/.agents/skills` (and `~/.codex/skills`)
- raises Claude Code transcript retention (`cleanupPeriodDays: 3650`; default is 30 days)
- macOS: installs a daily launchd job; Linux: prints a crontab line

Then run the one-time bootstrap in a repo:

```bash
claude "Use the agent-backprop skill in bootstrap mode for this repo"
```

## Layout

| Path | What |
|---|---|
| `SKILL.md` | workflow: extract → triage → cluster → route → staleness check → ledger edit → replay → tier → digest |
| `scripts/extract.py` | parses Claude Code + Codex logs, groups by repo, emits signals + per-session metrics (redacts secrets) |
| `scripts/ledger.py` | backups, tiers, proposals, metric check, rollback |
| `scripts/replay_eval.py` | blind A/B replay: would the new instructions have pre-empted the correction? |
| `scripts/run_daily.sh` | unattended headless run (Claude or Codex) |
| `references/` | signal taxonomy, routing rules, digest template, setup notes |
| `tests/smoke.sh` | offline end-to-end test on synthetic logs |

## Status

Scripts are tested on synthetic Claude Code / Codex logs (`tests/smoke.sh`). The LLM-driven steps (triage,
routing, digest) have not yet been run end-to-end on real history. Before the first scheduled run, verify on
your versions: `--allowedTools` syntax in `run_daily.sh`, `codex exec -` stdin support, and Codex writable roots.

## License

MIT — see [LICENSE](LICENSE).
