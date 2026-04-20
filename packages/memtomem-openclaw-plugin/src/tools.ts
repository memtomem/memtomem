/**
 * memtomem MCP tool definitions for OpenClaw registration.
 *
 * Each tool maps directly to a memtomem MCP server tool.
 * Schemas use TypeBox Type.Unsafe() to pass JSON Schema through.
 */

import { Type, type TSchema } from "@sinclair/typebox";

export interface ToolDef {
  name: string;
  description: string;
  parameters: TSchema;
}

// -- Helpers ------------------------------------------------------------------

const Str = (desc: string) => ({ type: "string" as const, description: desc });
const OptStr = (desc: string) => ({
  type: "string" as const,
  description: desc,
});
const Num = (desc: string, def?: number) =>
  def !== undefined
    ? { type: "number" as const, description: desc, default: def }
    : { type: "number" as const, description: desc };
const OptNum = (desc: string, def?: number) =>
  def !== undefined
    ? { type: "number" as const, description: desc, default: def }
    : { type: "number" as const, description: desc };
const Bool = (desc: string, def?: boolean) =>
  def !== undefined
    ? { type: "boolean" as const, description: desc, default: def }
    : { type: "boolean" as const, description: desc };

function schema(
  props: Record<string, object>,
  required: string[] = [],
): TSchema {
  return Type.Unsafe({
    type: "object",
    properties: props,
    required,
    additionalProperties: false,
  });
}

function emptySchema(): TSchema {
  return Type.Unsafe({
    type: "object",
    properties: {},
    additionalProperties: false,
  });
}

// -- Tool Definitions ---------------------------------------------------------

export const TOOLS: ToolDef[] = [
  // ── Search & Retrieval ─────────────────────────────────────────────────
  {
    name: "mem_search",
    description:
      "Search across indexed memory files using hybrid BM25 + semantic search",
    parameters: schema(
      {
        query: Str("Natural language search query"),
        top_k: Num("Number of results to return", 10),
        source_filter: OptStr(
          "Filter by source file path (substring match or glob)",
        ),
        tag_filter: OptStr("Comma-separated tags (OR logic)"),
        namespace: OptStr("Namespace scope"),
        bm25_weight: Num("Override BM25 weight in RRF fusion (default 1.0)"),
        dense_weight: Num("Override dense/semantic weight in RRF fusion (default 1.0)"),
      },
      ["query"],
    ),
  },
  {
    name: "mem_recall",
    description:
      "Recall memories created within a date range, ordered newest first",
    parameters: schema({
      since: OptStr("Inclusive start date (YYYY, YYYY-MM, YYYY-MM-DD, or ISO)"),
      until: OptStr("Exclusive end date"),
      source_filter: OptStr("Filter by source file path"),
      namespace: OptStr("Namespace scope"),
      limit: Num("Maximum chunks to return", 20),
    }),
  },

  // ── CRUD ───────────────────────────────────────────────────────────────
  {
    name: "mem_add",
    description:
      "Add a new memory entry to a markdown file and immediately index it",
    parameters: schema(
      {
        content: Str("The memory content to store"),
        title: OptStr("Optional heading title"),
        tags: {
          type: "array" as const,
          items: { type: "string" as const },
          description: "Optional tags for categorisation",
        },
        file: OptStr("Target .md filename (relative or absolute)"),
        namespace: OptStr("Namespace for indexed chunks"),
        template: OptStr("Built-in template: adr, meeting, debug, decision"),
      },
      ["content"],
    ),
  },
  {
    name: "mem_edit",
    description:
      "Edit an existing memory entry in its source markdown file and re-index",
    parameters: schema(
      {
        chunk_id: Str("UUID of the chunk to edit"),
        new_content: Str("The replacement content"),
      },
      ["chunk_id", "new_content"],
    ),
  },
  {
    name: "mem_delete",
    description:
      "Delete memory entries by chunk_id, source_file, or namespace",
    parameters: schema({
      chunk_id: OptStr("UUID of a specific chunk to delete"),
      source_file: OptStr("Path to remove all indexed chunks from"),
      namespace: OptStr("Namespace to delete all chunks from"),
    }),
  },
  {
    name: "mem_batch_add",
    description:
      "Add multiple memory entries in one call. Each entry: {key, value, tags?}",
    parameters: schema(
      {
        entries: {
          type: "array" as const,
          items: {
            type: "object" as const,
            properties: {
              key: { type: "string" as const, description: "Title" },
              value: { type: "string" as const, description: "Content" },
              tags: {
                type: "array" as const,
                items: { type: "string" as const },
              },
            },
            required: ["value"],
          },
          description: 'List of {key, value, tags?} entries',
        },
        namespace: OptStr("Namespace for all entries"),
        file: OptStr("Target .md file"),
      },
      ["entries"],
    ),
  },

  // ── Indexing ────────────────────────────────────────────────────────────
  {
    name: "mem_index",
    description: "Index or re-index markdown files for hybrid search",
    parameters: schema({
      path: { ...Str("File or directory path to index"), default: "." },
      recursive: Bool("Recurse into subdirectories", true),
      force: Bool("Re-index all files even if unchanged", false),
      namespace: OptStr("Namespace for indexed chunks"),
    }),
  },

  // ── Status & Config ────────────────────────────────────────────────────
  {
    name: "mem_stats",
    description: "Return current memory index statistics",
    parameters: emptySchema(),
  },
  {
    name: "mem_status",
    description: "Show indexing statistics and current configuration summary",
    parameters: emptySchema(),
  },
  {
    name: "mem_config",
    description: "View or update memtomem configuration values at runtime",
    parameters: schema({
      key: OptStr("Dot-notation config key (e.g. search.default_top_k)"),
      value: OptStr("New value to assign"),
    }),
  },
  {
    name: "mem_embedding_reset",
    description:
      "Check or resolve embedding configuration mismatches between DB and config",
    parameters: schema({
      mode: {
        ...Str("status | apply_current | revert_to_stored"),
        default: "status",
      },
    }),
  },

  // ── Namespace ──────────────────────────────────────────────────────────
  {
    name: "mem_ns_list",
    description: "List all namespaces and their chunk counts",
    parameters: emptySchema(),
  },
  {
    name: "mem_ns_get",
    description: "Get the current session namespace",
    parameters: emptySchema(),
  },
  {
    name: "mem_ns_set",
    description: "Set the session-default namespace",
    parameters: schema({ namespace: Str("Namespace to set") }, ["namespace"]),
  },
  {
    name: "mem_ns_delete",
    description: "Delete all chunks in a namespace from the index",
    parameters: schema({ namespace: Str("Namespace to delete") }, [
      "namespace",
    ]),
  },
  {
    name: "mem_ns_rename",
    description: "Rename a namespace",
    parameters: schema(
      {
        old: Str("Old namespace name"),
        new: Str("New namespace name"),
      },
      ["old", "new"],
    ),
  },
  {
    name: "mem_ns_info",
    description: "Get namespace information including metadata and chunk count",
    parameters: schema({ namespace: Str("Namespace to query") }, [
      "namespace",
    ]),
  },
  {
    name: "mem_ns_update",
    description: "Update namespace metadata (description and/or color)",
    parameters: schema(
      {
        namespace: Str("Namespace to update"),
        description: OptStr("Description text"),
        color: OptStr('Color hex code (e.g. "#6c5ce7")'),
      },
      ["namespace"],
    ),
  },

  // ── Dedup & Decay ──────────────────────────────────────────────────────
  {
    name: "mem_dedup_scan",
    description: "Scan for duplicate chunk candidates (dry-run, no mutations)",
    parameters: schema({
      threshold: Num("Cosine similarity threshold (0-1)", 0.92),
      limit: Num("Max candidate pairs to return", 50),
      max_scan: Num("Max chunks to inspect", 500),
    }),
  },
  {
    name: "mem_dedup_merge",
    description:
      "Merge duplicate chunks: keep keep_id, delete delete_ids. Tags are merged.",
    parameters: schema(
      {
        keep_id: Str("UUID of the chunk to keep"),
        delete_ids: {
          type: "array" as const,
          items: { type: "string" as const },
          description: "UUIDs of chunks to delete",
        },
      },
      ["keep_id", "delete_ids"],
    ),
  },
  {
    name: "mem_decay_scan",
    description: "Preview chunks that would be expired by TTL (dry-run)",
    parameters: schema({
      max_age_days: Num("Chunks older than this are listed", 90),
      source_filter: OptStr("Only scan matching sources"),
    }),
  },
  {
    name: "mem_decay_expire",
    description:
      "Delete chunks older than max_age_days. Defaults to dry_run=true.",
    parameters: schema({
      max_age_days: Num("Expiration threshold in days", 90),
      source_filter: OptStr("Only expire matching sources"),
      dry_run: Bool("Preview without deleting", true),
    }),
  },

  // ── Export & Import ────────────────────────────────────────────────────
  {
    name: "mem_export",
    description: "Export indexed memory chunks to a JSON bundle file",
    parameters: schema(
      {
        output_file: Str("Destination path for the JSON export"),
        source_filter: OptStr("Only export matching sources"),
        tag_filter: OptStr("Only export chunks with this tag"),
        since: OptStr("ISO 8601 datetime lower bound on created_at"),
        namespace: OptStr("Only export this namespace"),
      },
      ["output_file"],
    ),
  },
  {
    name: "mem_import",
    description:
      "Import memory chunks from a JSON bundle. Chunks are re-embedded with new UUIDs.",
    parameters: schema(
      {
        input_file: Str("Path to the JSON bundle file"),
        namespace: OptStr("Override namespace for imported chunks"),
      },
      ["input_file"],
    ),
  },

  // ── Auto-Tag ───────────────────────────────────────────────────────────
  {
    name: "mem_auto_tag",
    description:
      "Extract and apply keyword-based tags to indexed memory chunks",
    parameters: schema({
      source_filter: OptStr("Only process matching sources"),
      max_tags: Num("Max tags per chunk", 5),
      overwrite: Bool("Replace existing tags", false),
      dry_run: Bool("Preview without applying", false),
    }),
  },

  // ── Browse ─────────────────────────────────────────────────────────────
  {
    name: "mem_list",
    description:
      "List all indexed source files with chunk counts and metadata",
    parameters: schema({
      source_filter: OptStr("Filter by source file path"),
      namespace: OptStr("Only list sources in this namespace"),
    }),
  },
  {
    name: "mem_read",
    description:
      "Read the full content and metadata of a specific chunk by UUID",
    parameters: schema(
      { chunk_id: Str("UUID of the chunk") },
      ["chunk_id"],
    ),
  },

  // ── Tag Management ─────────────────────────────────────────────────────
  {
    name: "mem_tag_list",
    description: "List all tags and their usage counts, ordered by frequency",
    parameters: emptySchema(),
  },
  {
    name: "mem_tag_rename",
    description: "Rename a tag across all chunks that use it",
    parameters: schema(
      {
        old_tag: Str("Current tag name to replace"),
        new_tag: Str("New tag name"),
      },
      ["old_tag", "new_tag"],
    ),
  },
  {
    name: "mem_tag_delete",
    description: "Remove a tag from all chunks that use it",
    parameters: schema({ tag: Str("Tag name to remove") }, ["tag"]),
  },

  // ── URL Indexing ───────────────────────────────────────────────────────
  {
    name: "mem_fetch",
    description:
      "Fetch a URL, convert to markdown, and index it for search",
    parameters: schema(
      {
        url: Str("The URL to fetch and index"),
        tags: {
          type: "array" as const,
          items: { type: "string" as const },
          description: "Optional tags for indexed chunks",
        },
        namespace: OptStr("Namespace for indexed chunks"),
      },
      ["url"],
    ),
  },

  // ── Cross-Reference ────────────────────────────────────────────────────
  {
    name: "mem_link",
    description:
      "Create a bidirectional link between two chunks (related, supersedes, depends_on, contradicts)",
    parameters: schema(
      {
        source_id: Str("UUID of the first chunk"),
        target_id: Str("UUID of the second chunk"),
        relation_type: {
          ...Str("Type of relationship"),
          default: "related",
        },
      },
      ["source_id", "target_id"],
    ),
  },
  {
    name: "mem_unlink",
    description: "Remove a link between two chunks",
    parameters: schema(
      {
        source_id: Str("UUID of the first chunk"),
        target_id: Str("UUID of the second chunk"),
      },
      ["source_id", "target_id"],
    ),
  },
  {
    name: "mem_related",
    description:
      "Find all chunks linked to the given chunk with relationship types",
    parameters: schema(
      { chunk_id: Str("UUID of the chunk to find relations for") },
      ["chunk_id"],
    ),
  },

  // ── Episodic Memory (Sessions) ─────────────────────────────────────
  {
    name: "mem_session_start",
    description:
      "Start a new episodic memory session. All tool calls are tracked as events.",
    parameters: schema({
      agent_id: { ...Str("Agent identifier"), default: "default" },
      namespace: OptStr("Session namespace"),
    }),
  },
  {
    name: "mem_session_end",
    description:
      "End the current session with optional summary. Cleans up working memory.",
    parameters: schema({
      summary: OptStr("Summary of what was accomplished"),
    }),
  },
  {
    name: "mem_session_list",
    description: "List recent episodic memory sessions",
    parameters: schema({
      agent_id: OptStr("Filter by agent"),
      since: OptStr("Only sessions after this date (YYYY-MM-DD)"),
      limit: Num("Maximum sessions to return", 10),
    }),
  },

  // ── Working Memory (Scratchpad) ────────────────────────────────────
  {
    name: "mem_scratch_set",
    description:
      "Store a key-value in working memory (scratchpad) with optional TTL",
    parameters: schema(
      {
        key: Str("Unique key"),
        value: Str("Value to store"),
        ttl_minutes: Num("Auto-expire after N minutes"),
      },
      ["key", "value"],
    ),
  },
  {
    name: "mem_scratch_get",
    description: "Retrieve a value from working memory",
    parameters: schema({ key: Str("Key to look up") }, ["key"]),
  },
  {
    name: "mem_scratch_list",
    description: "List all entries in working memory",
    parameters: emptySchema(),
  },
  {
    name: "mem_scratch_promote",
    description:
      "Promote a working memory entry to long-term memory via mem_add",
    parameters: schema(
      {
        key: Str("Working memory key to promote"),
        title: OptStr("Title for the long-term entry"),
        tags: {
          type: "array" as const,
          items: { type: "string" as const },
          description: "Optional tags",
        },
        file: OptStr("Target file for the entry"),
      },
      ["key"],
    ),
  },

  // ── Procedural Memory ──────────────────────────────────────────────
  {
    name: "mem_procedure_save",
    description:
      "Save a reusable procedure (workflow/pattern) with trigger and steps",
    parameters: schema(
      {
        name: Str("Procedure name"),
        steps: Str("Step-by-step instructions"),
        trigger: OptStr("When to use this procedure"),
        tags: {
          type: "array" as const,
          items: { type: "string" as const },
          description: "Additional tags",
        },
        namespace: OptStr("Namespace"),
      },
      ["name", "steps"],
    ),
  },
  {
    name: "mem_procedure_search",
    description: "Search saved procedures (chunks tagged with 'procedure')",
    parameters: schema(
      {
        query: Str("What procedure are you looking for?"),
        top_k: Num("Maximum results", 5),
      },
      ["query"],
    ),
  },
  {
    name: "mem_procedure_list",
    description: "List all saved procedures",
    parameters: emptySchema(),
  },

  // ── Multi-Agent Memory ─────────────────────────────────────────────
  {
    name: "mem_agent_register",
    description:
      "Register an agent with its own namespace (agent-runtime:{id}) for memory isolation",
    parameters: schema(
      {
        agent_id: Str("Unique agent identifier"),
        description: OptStr("Agent role description"),
        color: OptStr("Color hex code for UI"),
      },
      ["agent_id"],
    ),
  },
  {
    name: "mem_agent_search",
    description:
      "Search with multi-agent scope (agent namespace + shared)",
    parameters: schema(
      {
        query: Str("Search query"),
        agent_id: OptStr("Agent ID (omit for current)"),
        include_shared: Bool("Also search shared namespace", true),
        top_k: Num("Maximum results", 10),
      },
      ["query"],
    ),
  },
  {
    name: "mem_agent_share",
    description:
      "Share a memory chunk to another agent or the shared namespace",
    parameters: schema(
      {
        chunk_id: Str("UUID of chunk to share"),
        target: {
          ...Str("Target namespace (shared or agent-runtime:{id})"),
          default: "shared",
        },
      },
      ["chunk_id"],
    ),
  },

  // ── Consolidation ──────────────────────────────────────────────────
  {
    name: "mem_consolidate",
    description:
      "Find groups of related chunks that can be summarized (dry-run)",
    parameters: schema({
      namespace: OptStr("Scope to namespace"),
      source_filter: OptStr("Filter by source path"),
      max_groups: Num("Maximum groups to return", 5),
      min_group_size: Num("Minimum chunks per group", 3),
    }),
  },
  {
    name: "mem_consolidate_apply",
    description:
      "Create a summary chunk for a group identified by mem_consolidate",
    parameters: schema(
      {
        group_id: Num("Group ID from mem_consolidate"),
        summary: Str("Agent-written summary text"),
        keep_originals: Bool("Keep original chunks", true),
      },
      ["group_id", "summary"],
    ),
  },

  // ── Reflection ─────────────────────────────────────────────────────
  {
    name: "mem_reflect",
    description:
      "Analyze memory patterns: access frequency, themes, gaps, clusters",
    parameters: schema({
      namespace: OptStr("Scope to namespace"),
      since: OptStr("Only analyze after this date"),
      limit: Num("Max items per category", 20),
    }),
  },
  {
    name: "mem_reflect_save",
    description: "Save a reflection insight derived from memory analysis",
    parameters: schema(
      {
        insight: Str("The insight or observation"),
        related_chunks: {
          type: "array" as const,
          items: { type: "string" as const },
          description: "Chunk UUIDs that informed this insight",
        },
        tags: {
          type: "array" as const,
          items: { type: "string" as const },
          description: "Additional tags",
        },
      },
      ["insight"],
    ),
  },

  // ── Evaluation ─────────────────────────────────────────────────────
  {
    name: "mem_eval",
    description:
      "Memory health report: access coverage, tag coverage, namespace stats, session activity",
    parameters: schema({
      since: OptStr("Only analyze after this date"),
      namespace: OptStr("Scope to namespace"),
    }),
  },

  // ── Search History ────────────────────────────────────────────────
  {
    name: "mem_search_history",
    description: "List past search queries with result counts",
    parameters: schema({
      limit: OptNum("Max queries to return", 20),
      since: OptStr("ISO date filter"),
    }),
  },
  {
    name: "mem_search_suggest",
    description: "Autocomplete search queries from history",
    parameters: schema({ prefix: Str("Query prefix to match"), limit: OptNum("Max suggestions", 5) }, ["prefix"]),
  },

  // ── Conflict Detection ────────────────────────────────────────────
  {
    name: "mem_conflict_check",
    description:
      "Check for contradictions between new content and existing memories (high similarity + low text overlap)",
    parameters: schema(
      {
        content: Str("Content to check against existing memories"),
        threshold: OptNum("Min similarity to flag", 0.75),
      },
      ["content"],
    ),
  },

  // ── Importance Scoring ────────────────────────────────────────────
  {
    name: "mem_importance_scan",
    description:
      "Compute and update importance scores for all chunks (access + tags + relations + recency)",
    parameters: schema({
      namespace: OptStr("Scope to namespace"),
    }),
  },

  // ── Q&A ───────────────────────────────────────────────────────────
  {
    name: "mem_ask",
    description:
      "Ask a question and get an answer grounded in your memories — searches for context and structures a Q&A prompt with cited sources",
    parameters: schema(
      {
        question: Str("The question to answer from your memories"),
        top_k: OptNum("Number of memory chunks for context", 5),
        namespace: OptStr("Scope to namespace"),
        source_filter: OptStr("Filter by source file path"),
        tag_filter: OptStr("Filter by tags (comma-separated)"),
      },
      ["question"],
    ),
  },

  // ── Importers ─────────────────────────────────────────────────────
  {
    name: "mem_import_notion",
    description:
      "Import a Notion export (ZIP or directory) — cleans UUID filenames, property tables, broken links and indexes for search",
    parameters: schema(
      {
        path: Str("Path to Notion export ZIP file or extracted directory"),
        namespace: OptStr("Namespace for imported content (default: notion)"),
        tags: {
          type: "array" as const,
          items: { type: "string" as const },
          description: "Tags to apply to all imported chunks",
        },
      },
      ["path"],
    ),
  },
  {
    name: "mem_import_obsidian",
    description:
      "Import an Obsidian vault — converts [[wikilinks]], ![[embeds]], callouts to standard markdown and indexes for search",
    parameters: schema(
      {
        vault_path: Str("Path to Obsidian vault root directory"),
        namespace: OptStr("Namespace for imported content (default: obsidian)"),
        tags: {
          type: "array" as const,
          items: { type: "string" as const },
          description: "Tags to apply to all imported chunks",
        },
      },
      ["vault_path"],
    ),
  },
];
