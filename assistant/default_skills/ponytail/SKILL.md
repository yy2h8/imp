---
name: ponytail
description: "Use when designing technical solutions or automations, writing or changing code, fixing bugs, reviewing implementations, or choosing tools and dependencies. Also use when the owner requests the simplest solution or asks to reduce complexity."
license: MIT
---

# Ponytail

Choose the smallest readable solution that fully meets the owner's requirements.
Optimize for clear, maintainable work and few moving parts. Apply this guidance
to the relevant task; it does not establish a persistent chat mode.

## Choose the simplest solution

Understand the requested outcome and inspect the relevant files and existing
flow before choosing an approach. For a bug, trace the cause and check callers
of the code you intend to change; fix the shared cause where appropriate.

Use the first option that meets the requirements:

1. An existing assistant tool, setting, or workflow already does the job.
   For example, use `schedule_job` for supported scheduling needs.
2. Existing code or a script can be reused or adjusted.
3. The standard library, a native platform feature, or an installed dependency
   provides the needed behavior.
4. Write the minimum new code required. Add a dependency when it makes the
   correct solution meaningfully simpler than maintaining custom code.

Use the operating manual's probed tools, shell, and resource limits. Prefer
a short readable script over a dense shell one-liner. Keep reusable scripts
in `scripts/` and disposable experiments in `scratch/`.

## Keep changes focused

- Fulfill the requested scope. Remove speculative features, wrappers, and
  configuration; do not silently replace explicit requirements with a partial
  solution or make the owner ask twice for the complete task.
- Follow existing patterns. Introduce abstractions when they solve a concrete
  problem; avoid scaffolding for hypothetical future needs and unrelated cleanup.
- Preserve input validation, security, error handling that prevents data loss,
  and accessibility. Keep calibration or configuration needed for real operating
  conditions.
- When deliberately accepting a meaningful limitation, leave a short `ponytail:`
  comment naming the limit and when to revisit it. Ordinary simple code needs
  no justification comment.

A request for advice, a design, or brainstorming ends with that result. It does
not authorize implementation or scheduling. When execution is already requested,
proceed within that scope without adding a design-approval ceremony. Use sensible
defaults for reversible choices; use `ask` when essential information is missing
and a wrong guess would be costly. Non-interactive runs cannot wait for replies;
report the blocker if the task cannot be completed with safe assumptions.

## Verify and deliver

Verify the behavior that matters, including relevant failure cases. For changes
to non-trivial logic, add or update a focused runnable check using the project's
existing test setup; a standalone script may use a small self-check. Scale checks
to risk and run the project's required checks. A short implementation can still
need several tests.

Lead the Telegram reply with the result, followed by what was verified and any
material limitation. Use concise plain text and provide more explanation when
requested. Deliver files the owner should have with `send_file`; include code
in chat when the owner asks for it or a short snippet is the useful result.
