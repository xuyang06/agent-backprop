#!/usr/bin/env bash
# Install the agent-backprop skill for Claude Code and Codex.
#   ./install.sh [--repo PATH]... [--schedule] [--hour 3] [--minute 17]
# - copies the skill to ~/.claude/skills/agent-backprop
# - symlinks it into ~/.agents/skills and ~/.codex/skills (Codex)
# - raises Claude Code transcript retention (cleanupPeriodDays) so history isn't deleted after 30 days
# - writes ~/.agent-backprop/config.json with the repos to reflect on
# - --schedule: installs a daily launchd job (macOS) or prints a crontab line (Linux)
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd -P)"
DEST="$HOME/.claude/skills/agent-backprop"
STATE="$HOME/.agent-backprop"
REPOS=(); SCHEDULE=0; HOUR=3; MINUTE=17
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPOS+=("$(cd "$2" && pwd -P)"); shift 2;;
    --schedule) SCHEDULE=1; shift;;
    --hour) HOUR="$2"; shift 2;;
    --minute) MINUTE="$2"; shift 2;;
    *) echo "unknown arg $1"; exit 1;;
  esac
done

mkdir -p "$HOME/.claude/skills" "$STATE"
if [ "$SRC" != "$DEST" ]; then rm -rf "$DEST"; cp -R "$SRC" "$DEST"; fi
chmod +x "$DEST"/scripts/*.py "$DEST"/scripts/*.sh
for d in "$HOME/.agents/skills" "$HOME/.codex/skills"; do
  if [ "$d" = "$HOME/.codex/skills" ] && [ ! -d "$HOME/.codex" ]; then continue; fi
  mkdir -p "$d"; ln -sfn "$DEST" "$d/agent-backprop"
done
echo "✓ skill installed at $DEST (linked for Codex)"

# Transcript retention. 0 would DISABLE transcript writing, so use a large number instead.
python3 - "$HOME/.claude/settings.json" <<'EOF'
import json, os, shutil, sys
p = sys.argv[1]
d = {}
if os.path.exists(p):
    shutil.copy2(p, p + ".bak-agent-backprop")
    with open(p) as fh:
        d = json.load(fh)
cur = d.get("cleanupPeriodDays")
if cur is None or (isinstance(cur, int) and 0 < cur < 365):
    d["cleanupPeriodDays"] = 3650
    with open(p, "w") as fh:
        json.dump(d, fh, indent=2)
    print(f"✓ cleanupPeriodDays {cur} -> 3650 in {p}")
else:
    print(f"✓ cleanupPeriodDays already {cur}")
EOF

python3 - "$STATE/config.json" "${REPOS[@]:-}" <<'EOF'
import json, os, sys
p, repos = sys.argv[1], [r for r in sys.argv[2:] if r]
c = json.load(open(p)) if os.path.exists(p) else {}
c["repos"] = sorted(set(c.get("repos", []) + repos))
json.dump(c, open(p, "w"), indent=2)
print(f"✓ repos: {c['repos'] or '(all history, user-level only)'}")
EOF

if [ "$SCHEDULE" = 1 ]; then
  if [ "$(uname)" = "Darwin" ]; then
    PL="$HOME/Library/LaunchAgents/com.user.agent-backprop.plist"
    cat > "$PL" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.user.agent-backprop</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$DEST/scripts/run_daily.sh</string><string>daily</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer></dict>
  <key>StandardOutPath</key><string>$STATE/logs/launchd.out</string>
  <key>StandardErrorPath</key><string>$STATE/logs/launchd.err</string>
</dict></plist>
EOF
    mkdir -p "$STATE/logs"
    launchctl unload "$PL" 2>/dev/null || true
    launchctl load "$PL"
    echo "✓ launchd job loaded: daily at $HOUR:$(printf %02d "$MINUTE") (runs at next wake if the Mac was asleep)"
  else
    echo "Add this line with 'crontab -e':"
    echo "$MINUTE $HOUR * * * /bin/bash $DEST/scripts/run_daily.sh daily"
  fi
fi

cat <<EOF

Next: run the one-time bootstrap (interactive, you review one report):
  cd <repo> && claude "Use the agent-backprop skill in bootstrap mode for this repo"
or unattended:
  $DEST/scripts/run_daily.sh bootstrap
EOF
