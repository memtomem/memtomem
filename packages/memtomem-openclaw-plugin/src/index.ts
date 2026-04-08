/**
 * memtomem OpenClaw plugin — bridges the memtomem MCP server into OpenClaw.
 *
 * Architecture:
 *   OpenClaw agent  ──▶  registerTool()  ──▶  McpBridge  ──▶  memtomem-server (subprocess)
 *
 * The MCP server is started lazily on the first tool call and stays running
 * for the lifetime of the gateway process. A background service handles
 * graceful shutdown.
 */

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { McpBridge } from "./bridge.js";
import { TOOLS } from "./tools.js";

export default definePluginEntry({
  id: "memtomem",
  name: "memtomem",
  description:
    "Markdown-first semantic memory — hybrid BM25 + dense search across your files",

  register(api) {
    const config = api.config as {
      command?: string;
      serverArgs?: string[];
    } | undefined;

    const bridge = new McpBridge({
      command: config?.command,
      args: config?.serverArgs,
    });

    // Register all 52 memtomem tools as OpenClaw agent tools.
    // Each tool proxies the call to the MCP server via stdio.
    for (const tool of TOOLS) {
      api.registerTool({
        name: tool.name,
        description: tool.description,
        parameters: tool.parameters,
        async execute(_id: string, params: Record<string, unknown>) {
          const text = await bridge.callTool(tool.name, params);
          return { content: [{ type: "text" as const, text }] };
        },
      });
    }

    // Background service for lifecycle management.
    api.registerService({
      name: "memtomem-mcp-bridge",
      async start() {
        // No-op: bridge connects lazily on first tool call.
      },
      async stop() {
        await bridge.close();
      },
    });
  },
});
