Call `mem_search` with the requested topic and use the compact output unless machine-readable details are necessary.

Present the strongest matches concisely with their source path, heading, and relevance score. Explain that memtomem uses BM25 by default and adds dense retrieval only when embeddings are configured. If nothing matches, suggest a broader query or the status workflow; do not write or index anything automatically.
