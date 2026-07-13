import { access } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import type { Config, PluginInput, PluginModule } from "@opencode-ai/plugin";

import {
  CORE_VERSION,
  MCP_TIMEOUT_MS,
  OPENCODE_COMMANDS,
  OPENCODE_READ_SKILLS,
  TOOL_MODE,
} from "./generated.js";

type PermissionValue = "allow" | "ask" | "deny";
type PermissionConfig = PermissionValue | Record<string, PermissionValue>;
type CommandConfig = { description: string; template: string };
type RuntimeConfig = Omit<Config, "mcp" | "command" | "skills" | "permission"> & {
  mcp?: Record<string, unknown>;
  command?: Record<string, unknown>;
  skills?: { paths?: string[]; urls?: string[] };
  permission?: PermissionConfig;
};
type ServerInput = Pick<PluginInput, "directory" | "worktree">;

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

export const SAFE_PERMISSIONS: Record<string, PermissionValue> = {
  "memtomem_*": "ask",
  memtomem_mem_search: "allow",
  memtomem_mem_recall: "allow",
  memtomem_mem_status: "allow",
  memtomem_mem_stats: "allow",
  memtomem_mem_list: "allow",
  memtomem_mem_read: "allow",
  memtomem_mem_do: "deny",
};

function mergePermissions(existing?: PermissionConfig): Record<string, PermissionValue> {
  if (typeof existing === "string") {
    if (existing === "deny") return { "*": "deny" };
    if (existing === "ask") return { "*": "ask", memtomem_mem_do: "deny" };
    return { "*": existing, ...SAFE_PERMISSIONS };
  }
  const generic: Record<string, PermissionValue> = {};
  const memtomem: Record<string, PermissionValue> = {};
  for (const [pattern, value] of Object.entries(existing ?? {})) {
    (pattern.startsWith("memtomem_") ? memtomem : generic)[pattern] = value;
  }
  return { ...generic, ...SAFE_PERMISSIONS, ...memtomem };
}

function ancestors(start: string): string[] {
  const result: string[] = [];
  let current = resolve(start);
  for (;;) {
    result.push(current);
    const parent = dirname(current);
    if (parent === current) return result;
    current = parent;
  }
}

async function exists(path: string): Promise<boolean> {
  try {
    await access(path);
    return true;
  } catch {
    return false;
  }
}

async function hasUserSkill(
  name: string,
  input: ServerInput,
  configuredPaths: readonly string[],
): Promise<boolean> {
  const roots = new Set<string>();
  for (const start of [input.directory, input.worktree].filter((item): item is string => !!item)) {
    for (const parent of ancestors(start)) {
      for (const relative of [
        ".opencode/skills",
        ".opencode/skill",
        ".claude/skills",
        ".agents/skills",
      ]) roots.add(join(parent, relative));
    }
  }
  for (const relative of [
    ".config/opencode/skills",
    ".config/opencode/skill",
    ".claude/skills",
    ".agents/skills",
  ]) roots.add(join(homedir(), relative));

  for (const configured of configuredPaths) {
    const absolute = resolve(configured);
    if (absolute.startsWith(packageRoot)) continue;
    if (absolute.endsWith(name) && await exists(join(absolute, "SKILL.md"))) return true;
    roots.add(absolute);
  }
  for (const root of roots) {
    if (await exists(join(root, name, "SKILL.md"))) return true;
  }
  return false;
}

async function configure(config: RuntimeConfig, input: ServerInput): Promise<void> {
  config.mcp ??= {};
  if (!("memtomem" in config.mcp)) {
    config.mcp.memtomem = {
      type: "local",
      command: ["uvx", "--from", `memtomem==${CORE_VERSION}`, "memtomem-server"],
      enabled: true,
      timeout: MCP_TIMEOUT_MS,
      environment: { MEMTOMEM_TOOL_MODE: TOOL_MODE },
    };
  }

  config.command ??= {};
  for (const [name, command] of Object.entries(OPENCODE_COMMANDS)) {
    if (!(name in config.command)) config.command[name] = command satisfies CommandConfig;
  }

  config.skills ??= {};
  config.skills.paths ??= [];
  for (const name of OPENCODE_READ_SKILLS) {
    const path = join(packageRoot, "skills", name);
    if (!config.skills.paths.includes(path) && !(await hasUserSkill(name, input, config.skills.paths))) {
      config.skills.paths.push(path);
    }
  }
  config.permission = mergePermissions(config.permission);
}

const plugin = {
  id: "opencode-memtomem",
  async server(input: PluginInput) {
    return {
      async config(config: Config) {
        await configure(config as RuntimeConfig, input);
      },
    };
  },
} satisfies PluginModule;

export default plugin;
