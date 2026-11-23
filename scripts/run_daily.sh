#!/usr/bin/env bash
# Unattended agent-backprop run. Called by launchd/cron (see references/setup.md).
#   run_daily.sh [daily|bootstrap]
# Env:
#   AGENT_BACKPROP_AGENT=claude|codex   (default claude)
#   AGENT_BACKPROP_STATE=~/.agent-backprop
set -euo pipefail
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

MODE="${1:-daily}"
STATE="${AGENT_BACKPROP_STATE:-$HOME/.agent-backprop}"
AGENT="${AGENT_BACKPROP_AGENT:-claude}"
SR="$(cd "$(dirname "$0")/.." && pwd -P)"
mkdir -p "$STATE/logs" "$STATE/reports"
cd "$STATE"
LOG="$STATE/logs/$(date +%F)-$MODE.log"
LOCK="$STATE/.run.lock"

# single instance
if ! mkdir "$LOCK" 2>/dev/null; then echo "another run in progress" >>"$LOG"; exit 0; fi
trap 'rmdir "$LOCK"' EXIT

# repos to grant write access to (instruction files / repo skills), from config.json
REPOS=()
if [ -f "$STATE/config.json" ]; then
  while IFS= read -r r; do [ -n "$r" ] && REPOS+=("$r"); done < <(python3 -c '
import json,os,sys
c=json.load(open(sys.argv[1]))
for r in c.get("repos",[]): print(os.path.expanduser(r))' "$STATE/config.json")
fi

PROMPT="[agent-backprop-run] Use the agent-backprop skill (at $SR) in $MODE mode. Unattended: do not ask questions; apply the tier rules; run the ledger check; write the digest and finish with the one-line summary."
# Daily runs read ALL history (user-level preferences come from every repo); bootstrap is limited to configured repos.
REPO_ARGS=""
for r in "${REPOS[@]:-}"; do [ -n "$r" ] && REPO_ARGS="$REPO_ARGS --repo $r"; done
if [ -n "$REPO_ARGS" ]; then
  if [ "$MODE" = "bootstrap" ]; then PROMPT="$PROMPT Pass these to extract.py:$REPO_ARGS"
  else PROMPT="$PROMPT Repos with write access for repo-level lessons:$REPO_ARGS"; fi
fi

echo "=== $(date) $AGENT $MODE ===" >>"$LOG"
if [ "$AGENT" = "claude" ]; then
  # Least privilege: only this skill's scripts may run via Bash; file edits auto-accepted.
  # If your Claude Code version uses a different permission-rule syntax, adjust --allowedTools.
  ALLOWED="Read,Edit,Write,Glob,Grep,Bash(python3 $SR/scripts/extract.py:*),Bash(python3 $SR/scripts/ledger.py:*),Bash(python3 $SR/scripts/replay_eval.py:*)"
  ADD_DIRS=(--add-dir "$HOME/.claude" --add-dir "$HOME/.codex" --add-dir "$HOME/.agents" --add-dir "$STATE")
  for r in "${REPOS[@]:-}"; do [ -n "$r" ] && ADD_DIRS+=(--add-dir "$r"); done
  claude -p "$PROMPT" --permission-mode acceptEdits --allowedTools "$ALLOWED" "${ADD_DIRS[@]}" >>"$LOG" 2>&1 || echo "claude exited $?" >>"$LOG"
else
  # workspace-write sandbox rooted at $STATE; Codex may need extra writable roots for ~/.claude and repos.
  codex exec --full-auto -C "$STATE" "$PROMPT" >>"$LOG" 2>&1 || echo "codex exited $?" >>"$LOG"
fi

# Ping only when something needs a decision.
REPORT="$(ls -t "$STATE"/reports/*.md 2>/dev/null | head -1 || true)"
if [ -n "$REPORT" ] && grep -qE "^## (Needs your decision|需要你决定)" "$REPORT"; then
  MSG="agent-backprop: decisions pending — $(basename "$REPORT")"
  if command -v osascript >/dev/null; then osascript -e "display notification \"$MSG\" with title \"Claude/Codex\"" || true
  elif command -v notify-send >/dev/null; then notify-send "Claude/Codex" "$MSG" || true; fi
fi
