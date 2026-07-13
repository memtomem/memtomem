Translate relative dates such as “last week” into `since` and `until` values when possible, then call `mem_recall`. Preserve any source, namespace, scope, or result-limit constraint from the request.

Present memories newest first with their creation date and source. Ask for a date range only when the request provides no usable temporal constraint. Use topic search, not recall, when the user is asking what a memory says rather than when it was created.
