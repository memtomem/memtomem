---
type: llm
focus: trace
weight: 2
---
You are grading what an AI agent stored in memory, from its tool-call trace.

Look only at calls to the memtomem `mem_add` tool (the tool name ends in `__mem_add`) whose
result says "Memory added to". Ignore calls that were refused or errored, and ignore what the
agent says in its replies. PASS only if at least one successful call's content (title, content
and tags together) records the fact below as **currently true**, stated affirmatively. FAIL if
the fact is missing or incomplete, recorded as an old or former value, negated, reversed, or
altered. Extra context or rationale in the note is fine.

The fact:
The release train cut-off is now Wednesdays at 14:00 KST, tracked in ticket RT-7741. (A different day or time, a time zone other than KST or one with no time zone, or Wednesday 14:00 recorded as the previous cut-off, all fail.)
