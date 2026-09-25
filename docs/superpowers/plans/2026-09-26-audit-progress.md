# Execution ledger — 2026-09-26 audit repairs

Plan: 2026-09-26-audit-repairs.md

Ruling: work in the existing tg-assistant branch with prior uncommitted repairs, as authorized by the current-branch task; no worktree copy or commits.
Preflight: delivery changes affect app, scheduler and uploads; update all callers together. Execution lock must precede active queue claim. Job conditional writes require unique revision identity, not content equality. File limits reuse existing configuration.
Task 1: in progress.

Tasks 1–9: implementation in progress; focused red→green evidence observed for config/session, owner/commands, plain text/retries, upload routing, first deadline/stale completion, writer closure/shell exit, cancellation/model outputs, filesystem bounds/symlinks, prompt reset/skill paths. Full current suite: 335 passed before final coverage additions.
Ruling: shared send_text is a module function plus transport method, so injected transports stay small.
Ruling: preserve plan semantics; no subagents until final review required by executing-plans skill.
Remaining: strengthen recovery/concurrency/limits tests, close generator lifetimes at consumers, finish schema validation, docs, wheel/Python matrix, final review.

Final review: independent reviewer found malformed tool argument objects and attachment schemas; both reproduced RED and repaired GREEN. Startup queue validation also moved before tailoring. Additional self-review reproduced question identity changing during persistence; fixed by binding delivery to the original future.
Tasks 1–9: code repairs implemented. Task 10: local final verification in progress; live Telegram/provider/service/Pi checks remain unverified, with concrete README smoke procedure.
Wheel-only verification: Python 3.13 isolated install in /tmp/imp-audit-wheel-only, imports resolved inside site-packages from /tmp; manual template present; imp --help exit 0. Python 3.12 frozen sync succeeded in /tmp/imp-audit-repairs-py312.
Ruling: active execution serialized; external actions and network sends are not exactly-once. No durable delivery service, new cancel command, or shell isolation added. Limits documented rather than inventing those features.

Final verification: 362 passed on Python 3.13.15 and 362 passed on Python 3.12.11. Ruff and git diff --check exit 0. Final rebuilt wheel reinstalled outside checkout: both imports, template, lossless Unicode split and CLI help verified.
Task 10: local verification complete; live smoke checks pending external credentials/explicit test scope and target-host access. No real Telegram messages, provider calls, service changes, commits or pushes made.
Ruling: bounded actual reads replace stat-only file size checks, preventing growth races from allocating the whole file. Full-file/shell resource enforcement remains the documented deployment boundary.
