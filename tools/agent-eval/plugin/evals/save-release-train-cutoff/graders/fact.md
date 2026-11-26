---
type: llm
focus: trace
weight: 2
---
You are grading what an AI agent stored in memory, from its tool-call trace.

Look only at calls to the memtomem `mem_add` tool (the tool name ends in `__mem_add`) whose
result says "Memory added to". Ignore calls that were refused or errored, results marked
"[idempotent replay]" (they wrote nothing), and what the agent says in its replies. Only the
title, content and tags arguments count as stored: text that appears only in other arguments,
such as template fields, may not be written. PASS only if at least one successful call's content (title, content
and tags together) records the fact below as **currently true**, stated affirmatively. FAIL if
the fact is missing or incomplete, recorded as an old or former value, negated, reversed, or
altered. Extra context or rationale in the note is fine.

The fact:
The release train cut-off is now Wednesdays at 14:00 KST, tracked in ticket RT-7741. (A different day or time, a time with no time zone, or Wednesday 14:00 recorded as the previous cut-off, all fail. The same instant written in another explicit time zone, such as Wednesdays 05:00 UTC, passes.)
