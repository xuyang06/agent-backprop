# Where a lesson goes

Pick the place the agent will actually read *at the moment the mistake would happen again*.
A skill is only loaded when it triggers, so a cross-task preference written into one skill silently fails
everywhere else. A repo file is shared with the team, so personal taste does not belong there.

| Lesson is about… | Target | Mirror to | Example |
|---|---|---|---|
| How the user likes output/interaction, any task | `~/.claude/CLAUDE.md` managed block | `~/.codex/AGENTS.md` managed block | "No emoji in written output." / "Don't ask which format; default to Markdown and mention it." |
| A fact about one repo | `<repo>/CLAUDE.md` managed block | `<repo>/AGENTS.md` if it exists (create only at `notify` tier) | "Tests: `make test-fast`; full suite needs Docker." |
| A procedure step for a kind of task a skill covers | that skill's `## Learned` | — (skills are shared via the symlinked folder) | report-writer: "Pull metrics from the dashboard export, not from memory." |
| An environment fact on this machine | `~/.claude/CLAUDE.md` (user) or repo file if repo-specific | AGENTS.md counterpart | "`python` is 3.9 here; use `python3.12`." |
| A repeated multi-step procedure with no covering skill | new skill (approve tier) | install symlink covers Codex | "release-notes": collect merged PRs, group by label, draft in house format |

Choosing among skills: prefer the skill in `skills_active` at the time of the correction. If none was
active but one *should* have triggered (its description covers the task), the problem may be triggering:
propose a `description` change (approve tier) rather than adding a Learned bullet nobody will load.

Repo vs user test: would a teammate working in this repo want this rule? Yes → repo. Only this user → user.
If unsure, choose user scope — it is private and reversible.

Codex note: Codex reads `AGENTS.md` (global `~/.codex/AGENTS.md`, plus repo files) rather than `CLAUDE.md`, and
loads user skills from `~/.agents/skills` (some versions also `~/.codex/skills`). Keep the two instruction
files' managed blocks identical in content.
