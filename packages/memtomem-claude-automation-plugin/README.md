# memtomem Automation for Claude Code

Optional automation for users who intentionally want prompt-time memory
retrieval and write-time indexing. Install the safe base plugin first.

```text
/plugin install memtomem@memtomem
/plugin install memtomem-automation@memtomem
```

The hooks use the memtomem CLI, pinned to the same reviewed core release:

```sh
uv tool install 'memtomem==0.3.9'
```

The dispatcher is launched through `uv`, so it does not depend on a
platform-specific `python3` command alias.

The plugin searches prompts longer than 20 characters, queues changed Write or
Edit targets for indexing, and flushes that queue when a response stops. It
does not create or close episodic sessions. Disable or uninstall this plugin
when automatic context injection or file indexing is not desired.
