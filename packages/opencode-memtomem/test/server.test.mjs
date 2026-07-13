import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import plugin from "../dist/server.js";

async function apply(config = {}, input = {}) {
  const hooks = await plugin.server(input);
  await hooks.config(config);
  return config;
}

test("installs exact MCP, six commands, three read skills, and safe permissions", async () => {
  const config = await apply();
  assert.deepEqual(config.mcp.memtomem.command, [
    "uvx", "--from", "memtomem==0.3.8", "memtomem-server",
  ]);
  assert.equal(config.mcp.memtomem.environment.MEMTOMEM_TOOL_MODE, "core");
  assert.equal(config.mcp.memtomem.timeout, 60000);
  assert.equal(Object.keys(config.command).length, 6);
  assert.equal(config.skills.paths.length, 3);
  assert.equal(config.permission.memtomem_mem_search, "allow");
  assert.equal(config.permission["memtomem_*"], "ask");
  assert.equal(config.permission.memtomem_mem_do, "deny");
});

test("preserves user MCP and commands and is idempotent", async () => {
  const mcp = { type: "remote", url: "https://example.test/mcp", enabled: false };
  const command = { template: "mine", description: "mine" };
  const config = { mcp: { memtomem: mcp }, command: { "memtomem-search": command } };
  await apply(config);
  await apply(config);
  assert.equal(config.mcp.memtomem, mcp);
  assert.equal(config.command["memtomem-search"], command);
  assert.equal(config.skills.paths.length, 3);
});

test("merges scalar and object permissions without weakening explicit rules", async () => {
  const denied = await apply({ permission: "deny" });
  assert.deepEqual(denied.permission, { "*": "deny" });

  const asked = await apply({ permission: "ask" });
  assert.deepEqual(asked.permission, { "*": "ask", memtomem_mem_do: "deny" });

  const allowed = await apply({ permission: "allow" });
  assert.equal(allowed.permission["*"], "allow");
  assert.equal(allowed.permission.memtomem_mem_add, undefined);
  assert.equal(allowed.permission["memtomem_*"], "ask");
  assert.equal(allowed.permission.memtomem_mem_do, "deny");

  const explicit = await apply({
    permission: { "*": "allow", memtomem_mem_search: "deny", memtomem_mem_add: "allow" },
  });
  assert.equal(explicit.permission.memtomem_mem_search, "deny");
  assert.equal(explicit.permission.memtomem_mem_add, "allow");
  assert.equal(explicit.permission.memtomem_mem_do, "deny");
});

test("does not shadow a project skill with the same name", async () => {
  const project = await mkdtemp(join(tmpdir(), "opencode-memtomem-"));
  const skill = join(project, ".opencode", "skills", "memtomem-search");
  await mkdir(skill, { recursive: true });
  await writeFile(join(skill, "SKILL.md"), "---\nname: memtomem-search\n---\n");
  const config = await apply({}, { directory: project, worktree: project });
  assert.equal(config.skills.paths.some((path) => path.endsWith("memtomem-search")), false);
  assert.equal(config.skills.paths.length, 2);
});
