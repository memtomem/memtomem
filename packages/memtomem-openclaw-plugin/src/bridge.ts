/**
 * MCP client bridge — manages a subprocess running memtomem-server
 * and communicates over stdio transport.
 *
 * Uses lazy connection: the MCP server is started on the first tool call,
 * not at plugin registration time.
 */

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

export interface BridgeConfig {
  command: string;
  args: string[];
  env?: Record<string, string>;
}

const DEFAULT_CONFIG: BridgeConfig = {
  command: "uvx",
  args: ["--from", "memtomem", "memtomem-server"],
};

export class McpBridge {
  private client: Client | null = null;
  private transport: StdioClientTransport | null = null;
  private connecting: Promise<void> | null = null;
  private config: BridgeConfig;

  constructor(config?: Partial<BridgeConfig>) {
    this.config = { ...DEFAULT_CONFIG, ...config };
  }

  /** Ensure the MCP client is connected, starting the server if needed. */
  private async ensureConnected(): Promise<Client> {
    if (this.client) return this.client;
    if (!this.connecting) {
      this.connecting = this.connect();
    }
    await this.connecting;
    return this.client!;
  }

  private async connect(): Promise<void> {
    this.transport = new StdioClientTransport({
      command: this.config.command,
      args: this.config.args,
      env: { ...process.env, ...this.config.env } as Record<string, string>,
    });

    this.client = new Client(
      { name: "memtomem-openclaw", version: "0.1.0" },
      { capabilities: {} },
    );

    await this.client.connect(this.transport);
  }

  /** Call an MCP tool and return the text content. */
  async callTool(
    name: string,
    args: Record<string, unknown>,
  ): Promise<string> {
    const client = await this.ensureConnected();
    const result = await client.callTool({ name, arguments: args });

    // Extract text from MCP content blocks
    const content = result.content as Array<{ type: string; text?: string }>;
    return content
      .filter((c) => c.type === "text" && c.text)
      .map((c) => c.text!)
      .join("\n");
  }

  /** Gracefully shut down the MCP server subprocess. */
  async close(): Promise<void> {
    if (this.client) {
      await this.client.close();
      this.client = null;
      this.transport = null;
      this.connecting = null;
    }
  }
}
