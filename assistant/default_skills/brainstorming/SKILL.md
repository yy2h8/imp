---
name: brainstorming
description: "Use when the owner asks to brainstorm, explore alternatives, or turn an underspecified idea into a practical plan. Skip routine requests whose outcome and approach are already clear."
---

# Brainstorming

Help the owner turn an open-ended idea into a useful direction and a concrete
next step. This applies to personal plans, writing, research, automation, and
software. Match the depth to the decision, not to a fixed process.

## Develop the idea

1. Start from the conversation and relevant materials. Identify the desired
   outcome, constraints, and what would make the result useful. Inspect files
   or research facts only when they inform the decision; a brainstorming
   request does not automatically need a repository review or web search.
2. Offer a useful starting point with explicit assumptions. Ask about gaps
   that would materially change the direction, rather than interviewing the
   owner about every detail. Prefer one focused question per exchange, with
   easy-to-answer choices when helpful.
3. Recommend the simplest approach that meets the goal. Compare alternatives
   when they offer real trade-offs, or when the owner requests breadth; do
   not invent extra options to fill a quota. Explain the relevant differences
   in effort, cost, convenience, or limitations.
4. Refine using the owner's replies; keep agreed decisions unless new facts
   change them. For a large idea, identify a useful first milestone before
   detailing the rest. For code, read the existing flow and reuse its patterns.
5. Converge on a short, actionable summary: the outcome, chosen direction,
   important constraints or assumptions, unresolved decisions, and next step.
   Keep unanswered questions visible instead of treating them as agreement.

## Fit the Telegram conversation

- Use short paragraphs and compact numbered choices. Messages are plain text;
  avoid tables, rendered diagrams, or formatting that needs a rich interface.
- When a follow-up question would help, put it at the end of the final reply
  and end the turn. The owner can continue in another message.
- Reserve `ask` for essential information needed before an authorized action
  where a wrong guess would be costly. It blocks the turn and delays other
  requests and scheduled jobs; do not use it for routine preference gathering.
- In scheduled or other non-interactive runs, do not ask questions or wait for
  approval. Produce useful options with stated assumptions; if essential
  information is missing, report what prevents completion.

## Finish at the requested scope

A request for ideas or a plan ends with ideas or a plan. It does not authorize
implementation, scheduling, or other follow-up actions. If the owner also asked
you to carry out the work, proceed within that scope once essential decisions
are settled. Do not ask again for approval already given.

Keep the result in chat by default. If the owner requests a saved plan or
handoff, write a concise note in `outbox/` (or their chosen workspace location)
and deliver it with `send_file`. Include enough context to use it without this
conversation. Formal specs, Git commits, and separate implementation plans are
not required by this skill.
