# Candidate kinds (from extract.py) and how to judge them

All kinds come from cheap heuristics. Expect false positives; the context fields decide.

| kind | Trigger | Real lesson looks like | Common false positive |
|---|---|---|---|
| `correction` | user follow-up matches correction phrases (EN/ZH) | "use uv, not pip", "reports never use emoji", "edit CONTRIBUTING.md, not README" | "no" answering a yes/no question; quoting an error message; a new request that just starts with "no worries" |
| `correction_after_done_claim` | correction right after the assistant claimed done/fixed/tests pass | a verification step was skipped: run tests / build / open the output before claiming success | user adds a new requirement after a genuinely finished task |
| `interrupt` | user pressed Esc (`[Request interrupted by user]`) or Codex `turn_aborted`, not followed by an explicit correction | the agent went the wrong way fast — wrong file, too broad, started before confirming scope on an expensive task | user interrupted to answer a phone call / changed their mind for unrelated reasons |
| `avoidable_question` | assistant ended with a question; user answered with a short affirmation/default | "for X, default to Y and say so in one line instead of asking" | the question was warranted (irreversible, costly, genuinely ambiguous) — keep the behavior |
| `self_correction` | assistant says it was wrong / "you're right" / 我搞错了 | earlier claim was made without checking; lesson = what to check first | sycophantic "you're right" when the assistant was actually right (that's its own problem, but not a lesson to write into a skill) |
| `retry_loop` | ≥2 consecutive failed tool calls, then a success | the command/flag/env fact that worked; or a script to bundle into a skill | flaky network; tests failing because the code was mid-edit |

Fields: `evidence` (the triggering text), `prev_assistant` (what the user was reacting to), `next_assistant`,
`first_user_prompt` (the replay request), `skills_active`, `repo`, `session`. `retry_loop` also has
`failed_commands`, `succeeded_command` (model-authored, trusted as data) and `untrusted_tool_evidence`.

## Generalizable or task-local?

Ask: would this rule have changed the agent's behavior in a *different* session? Signs it generalizes:
- the user references the past ("again", "I said", "always", "每次", "说过", "又")
- the same correction appears in several sessions (registry counts it)
- it is about a tool, format, convention, or default rather than about one specific file/value in one task

Write the lesson as an instruction with its reason, specific enough to act on:
- weak: "Be careful with package managers."
- good: "Install Python deps in this repo with `uv sync`; `pip install` fails (externally-managed env) and the user has corrected it repeatedly."

## Metrics in sessions.jsonl
`followup_user_turns`, `corrections`, `interrupts`, `avoidable_question_candidates`, `self_corrections`,
`false_done_candidates`, `retry_loops`, `tool_errors`, `rework_ratio` = (corrections+interrupts)/followups.
They are heuristic counts — use them for before/after comparison within the same scope, not as absolute truth.
