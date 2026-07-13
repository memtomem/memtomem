---
name: recall
description: Recall memtomem memories created in a date range. Use for recent memories or requests scoped by day, week, month, source, or namespace.
argument-hint: [date range]
allowed-tools: mcp__plugin_memtomem_memtomem__mem_recall, mcp__memtomem__mem_recall
---

# Recall memories

Use `$ARGUMENTS` as the date range or recent-memory request.
If the request does not clearly specify the date range or recent-memory request, ask before calling a tool.
Translate relative dates such as “last week” into `since` and `until` values when possible, then call `mem_recall`. Preserve any source, namespace, scope, or result-limit constraint from the request.

Present memories newest first with their creation date and source. Ask for a date range only when the request provides no usable temporal constraint. Use topic search, not recall, when the user is asking what a memory says rather than when it was created.
