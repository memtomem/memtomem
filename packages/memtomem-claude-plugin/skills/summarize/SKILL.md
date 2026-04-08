---
name: summarize
description: Summarize the current conversation and save key decisions, findings, and action items as a memory entry.
allowed-tools: mcp__memtomem__mem_add, mcp__memtomem__mem_search
disable-model-invocation: true
---

Summarize this conversation and save it as a memory.

## Instructions

1. Review the conversation and extract:
   - Key decisions made
   - Important findings or discoveries
   - Action items or next steps
   - Errors encountered and how they were resolved
   - Architecture or design choices

2. Skip trivial exchanges (greetings, confirmations, etc.)

3. Format as a structured summary using `mem_add` with the `decision` or `debug` template if applicable, or plain text:

```
mem_add(
  content="<structured summary>",
  title="Session: <brief topic>",
  tags=["session", "auto", "<relevant-topic-tags>"]
)
```

4. Before saving, run `mem_search` with a key phrase from the summary to check for duplicates. If a very similar entry exists, mention it but still save (the dedup system will catch exact matches).

5. Report what was saved and the file path.
