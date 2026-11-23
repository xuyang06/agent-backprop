#!/usr/bin/env bash
# Offline smoke test: fake HOME with Claude + Codex logs -> extract -> ledger -> mock replay -> rollback.
set -euo pipefail
SR="$(cd "$(dirname "$0")/.." && pwd -P)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
python3 "$SR/tests/make_fixtures.py" "$T" >/dev/null
S="$T/.agent-backprop"; SK="$T/code/acme/.claude/skills/py-setup/SKILL.md"
HOME="$T" python3 "$SR/scripts/extract.py" --mode bootstrap --claude-dir "$T/.claude/projects" \
  --codex-dir "$T/.codex/sessions" --state-dir "$S" > "$T/x.json"
python3 - "$T/x.json" <<'PY'
import json,sys; d=json.load(open(sys.argv[1]))
assert d["sessions_analyzed"]==3, d; assert d["skipped"].get("agent-backprop_run")==1, d
k=d["candidates_by_kind"]; assert k.get("retry_loop")==1 and k.get("correction_after_done_claim")==1, k
PY
grep -q "REDACTED" "$S"/runs/*/candidates.jsonl
cp "$SK" "$T/old.md"
python3 "$SR/scripts/ledger.py" --state-dir "$S" begin --id L-test --files "$SK" >/dev/null
printf '\n## Learned\n- Install deps with `uv sync`, not pip. <!-- sr:id=L-test n=2 -->\n' >> "$SK"
python3 "$SR/scripts/ledger.py" --state-dir "$S" finish --id L-test --tier auto --scope-kind skill --scope py-setup --signals correction >/dev/null
echo '{"id":"c","request":"install deps","later_feedback":"use uv not pip"}' > "$T/cases.jsonl"
python3 "$SR/scripts/replay_eval.py" --cases "$T/cases.jsonl" --old "$T/old.md" --new "$SK" --runner mock >/dev/null
python3 "$SR/scripts/ledger.py" --state-dir "$S" rollback --id L-test >/dev/null
cmp -s "$SK" "$T/old.md"
echo "smoke test passed"
