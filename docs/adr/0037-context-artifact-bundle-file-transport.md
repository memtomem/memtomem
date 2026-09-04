# ADR-0037: Sharing one context artifact as a file — bundle format and receipt gate

**Status:** Proposed
**Date:** 2026-09-04
**Context:** Issue #2298, split out of the Context Gateway drag-and-drop
discussion (#2297) as the "share outside this machine" question that accelerator
deliberately excluded.
**Extends:** [ADR-0023](0023-cross-project-artifact-transfer.md) — the
move/copy engine, whose locking, staging and promote skeleton receipt adapts,
with Gate A deliberately moved ahead of staging (§6)
**Qualifies:** [ADR-0006](0006-web-ui-folder-upload-redaction.md) Axis F (the
bundle-provenance marker, deliberately not adopted here)

## Context

There is no way to hand one skill, command, or agent to a colleague, or to
another machine of your own. Two indirect paths exist and neither answers the
question:

- move the artifact to `project_shared` and commit it — that shares a whole
  repository, and only with people who have it;
- `mm wiki push` — a thin `git push` over a separate wiki repository
  (ADR-0008), which shares the wiki, not an artifact.

No artifact bundle format exists anywhere in the context subsystem, and no
instance-to-instance transfer. The memory-side export bundle
(`tools/export_import.py`, `GET /api/export`) covers memory chunks: its records
are chunk-shaped, and its provenance key is bound to the memory database path
(`provenance.key_path_for_db`), so an artifact cannot ride it.

What the subsystem does have is a complete write-side transaction for moving one
artifact between stores — `context/transfer.py:transfer_artifact` — and a
complete egress precedent for copying artifact bytes into a host-global,
pushable location — `wiki/promote.py:promote_asset`. This ADR pins a file
transport that reuses both rather than inventing a third shape.

The privacy question is the reason this needs a decision rather than a patch.
ADR-0011 §5 puts the trust boundary at the write chokepoint and hard-refuses a
`project_shared` landing on a secret match with no `force_unsafe` valve, because
"git history is forever". A bundle handed to someone else is the same kind of
irreversible: the moment the file leaves, no local edit retracts it. Both
directions — what may be packed, and what may land — have to be settled against
that rule, not around it.

## Decision

### 1. Transport is a file, and only a file

`mm context export` writes a JSON bundle to a path the operator names;
`mm context import` reads one from a path. There is no listener, no outbound
connection, and no URL argument on either verb.

[ADR-0029](0029-mcp-network-transport-auth-stance.md) settles the listening
half: memtomem ships no first-party transport authentication, and the answer to
a future remote requirement is full OAuth 2.1, never a static token. Dialing out
is unaddressed there, and this ADR does not open it: a first-party uploader
would need a destination, a credential, and a retention story, none of which the
issue asks for.

Relationship to [ADR-0006](0006-web-ui-folder-upload-redaction.md) Axis G: that
axis is an **MCP-tool-only** question by its own terms — it governs which
filesystem locations `mem_export` may write to and `mem_import` may read from,
and it notes that the web transport exposes no operator-named path at all.
`mm context export` does write to an operator-named path, but from a CLI, where
the operator names the path themselves; it adds no MCP-reachable
arbitrary-destination write, which is the channel Axis G's residual and reopen
trigger are about. §10 is what keeps that true.

### 2. Bundle format v1 — its own shape, not `ExportBundle`

```json
{
  "format": "memtomem-context-artifact-bundle",
  "version": 1,
  "exported_at": "2026-09-04T12:00:00Z",
  "kind": "agents",
  "name": "reviewer",
  "source": {"tier": "project_shared", "wiki_commit": null},
  "versions_included": true,
  "payload_sha256": "<64 lowercase hex>",
  "provenance": null,
  "dirs": ["scripts/fixtures"],
  "files": [
    {"path": "agent.md", "exec": false, "sha256": "<64 lowercase hex>", "content_b64": "…"}
  ]
}
```

**Normative schema.** Every listed key is required and none may be null except
`source.wiki_commit` (40 lowercase hex or null) and `provenance` (always null
in v1). `format` is the exact literal; `version` is the integer 1; `kind` is
one of `agents` / `commands` / `skills`; `name` satisfies the store's
artifact-name rules **and**, because it becomes a path segment on the
receiver, the same portability rules the path grammar below applies — no
trailing dot, not a reserved device word, and not a forbidden component — and
is refused outright if it has the shape of an internal staging or move-aside
directory, which the name validator accepts today but every discovery walk
skips, so such an artifact would land invisible to `mm context status` and
unusable as a skill. Both rules apply to a `--as` landing name as well;
`source.tier` is one of the three tiers; `exported_at` is an RFC 3339 UTC
timestamp with a `Z` suffix; `versions_included` is a boolean that must agree
with whether the payload actually contains a version surface, and is
re-derived on receipt rather than trusted for anything but a mismatch refusal;
every digest is exactly 64 lowercase hex characters. `files` is sorted by
`path` and `dirs` likewise, and an unsorted array is refused rather than
re-sorted — a reader that re-sorts would accept two byte-different bundles as
the same artifact. Unknown **top-level** keys are tolerated so a later version
can add an informational field; unknown keys **inside `source` and inside a
`files` entry** are refused, because those are the security-relevant units and
a future behavior-carrying field must not be silently ignored by an older
reader. `source` has exactly the two keys `tier` and `wiki_commit`.

`dirs` lists directories that contain no files anywhere beneath them. Without
it, the payload writer — which creates only the parents of files it writes —
would silently drop an empty directory that `mm context copy` preserves, and a
skill may legitimately hold one.

**Path grammar is an allowlist, not a denylist.** Every path in `files` and
`dirs` is a POSIX-relative path whose segments each match
`[A-Za-z0-9._-]{1,64}`, do not begin with a hyphen, are not `.` or `..`, and do
not end in a dot; a path has at most 16 segments and 255 characters in total.
Two word-level refusals ride alongside, because a character allowlist cannot
express either. A segment whose text before its first dot is, case-insensitively,
`CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, or `LPT1`–`LPT9` is refused, since
Windows resolves those as devices whatever the extension. And any segment that a
conforming exporter is forbidden to emit is refused on receipt too: `.git`,
`.DS_Store`, `__pycache__`, or any segment ending in `.bak`. Without that, a
crafted bundle could land paths the install and dirty walkers ignore, which is a
place to stash bytes that no later integrity check looks at.

Both word-level rules are **one predicate, applied case-folded, to every path
segment and to the artifact `name` and any `--as` landing name alike**. The name
needs them for the same reason a segment does — it becomes a directory in the
store — and skipping it there would make `.git` a valid artifact name whose
`project_shared` landing creates `<store>/.git`, which git cannot track at all.
Case folding is what stops `.GIT` from aliasing `.git` on a case-insensitive
receiver, the same reason the topology rules fold before comparing.
This is deliberately narrower than what the store or any single operating system
allows. Enumerating what to *refuse* — Windows reserved device names, the
`? * " < > |` class, control characters, colon segments and alternate-data
streams, backslashes, trailing spaces, Unicode forms that normalize together —
is an open-ended list where a miss is a landed file the sender did not describe.
An allowlist has no such tail: anything outside it is refused on every platform,
with a message naming the offending path. Non-ASCII filenames are the price and
are refused loudly, not silently transliterated; carrying them needs a
normalization policy this ADR does not settle (see Consequences).

**Topology.** Every check below runs over the union of `files` and `dirs` with
each path first ASCII-case-folded component by component. No folded path may
repeat, and no folded path may be an ancestor of another **unless both are
`dirs` entries** — so a `files` entry may not prefix anything, and a `dirs`
entry may not prefix a file. Folding matters for ancestry and not only for
equality: file `A` and file `a/b` are distinct as written, yet on a
case-insensitive filesystem they demand that `A` be a file and a directory at
once. These are exactly the shapes that cannot be materialized consistently.

**`payload_sha256` binds the structure**, not just the contents. It is SHA-256
over the byte string formed by the domain-separation line
`memtomem-context-artifact-bundle/v1\n`, then the identity line
`i\n<kind>\n<name>\n` — both fields are inside the digest because both steer
validation, the destination, and the manifest rewrite, so leaving them out
would let a tampered bundle change where bytes land while every digest still
verified — then, for each `files` entry in array order, `f\n<path>\n<exec as 0
or 1>\n<sha256>\n`, then for each `dirs` entry `d\n<path>\n`, with every path
encoded as UTF-8 and every field terminated by a newline that the grammar
above forbids inside a path — so the framing is unambiguous without a length
prefix. A reordered, added, removed, renamed, or mode-flipped entry therefore
fails the bundle digest even when every surviving per-file content digest
still matches. It is deliberately not `skill_payload.payload_digest`, which is
the ADR-0030 §10 content-only tree digest and would notice none of those.

A bundle is always dir-layout. A legacy flat source (`agents/foo.md`) exports as
the single entry `_DIR_MANIFEST[kind]` (`agent.md`), so receipt never has to
choose between two collision identities for the same name.

Entries are regular files only: no symlink, device, or hard-link entry type. The
one piece of metadata carried is `exec`, the executable bit, because
`mm context copy` preserves it (`shutil.copytree(symlinks=True)` /
`copy2`) and so does wiki promotion; dropping it would silently strip a skill's
runnable script on receipt while the equivalent local copy kept it. It is inside
`payload_sha256` precisely because it changes what lands, and the per-file
content digest does not cover it.

Its mapping is normative in both directions, so two implementations cannot
disagree about what a received tree looks like: on export `exec` is
`(st_mode & 0o111) != 0`; on receipt a true `exec` lands the file at `0o755` and
a false one at `0o644`, matching wiki promotion, and on a platform with no
executable bit `exec` is recorded and ignored rather than refused. Every other
mode bit — setuid, setgid, sticky, group and other write — and every timestamp
is discarded, not carried: they are not part of what an artifact is, and
carrying them would put more of the sender's filesystem state inside the
receiver's tree than the format can validate.

### 3. Integrity, not authenticity — no provenance marker in v1

Each entry carries `sha256` and the bundle carries `payload_sha256`. Both are
**always recomputed** on read and both refuse the whole bundle on mismatch. They
detect transport corruption and tampering; they establish nothing about who
wrote the bytes, and no code path may use them to skip a scan.

ADR-0006 Axis F.3's HMAC marker is deliberately not adopted, because every
consumer it could have here is void:

- *skip the re-scan on a self-export* — receipt scans unconditionally (§5), and
  the same-machine case is already served by `mm context copy`;
- *honor `source.wiki_commit` by writing a pin* — a wiki commit SHA from
  another host proves nothing about the receiver's wiki, and `mm context adopt`
  already verifies received bytes against it (§9);
- *honor version labels* — those are content, validated on read (§9).

Dropping it also removes a memory-database dependency from a command that must
work on a machine that has never indexed anything: `load_or_create_key_for_export`
fails closed on a key-file anomaly, which would turn a missing or
wrong-permissioned sidecar into an export failure for a feature that does not
need the key at all.

The top-level `provenance` key is **reserved**: v1 writers emit `null` and v1
readers never branch on it. A future marker must use its own `SCHEME` string —
never `memtomem-bundle-provenance-v1`, whose key would then sign two different
payload types — and must bind `kind`, `name`, and the canonical structure
`payload_sha256` covers, per the rule stated in `provenance.py`'s module
docstring that any field import trust depends on lives inside the signed
payload.

### 4. Egress: hard refusal, no valve, from every source tier

Export resolves the source, acquires its canonical lock, runs the swap prelude
so an interrupted skills swap is resolved before anything reads the tree and
re-verifies the artifact is complete afterwards, reads each file once, scans
those in-memory bytes with `scope="project_shared"` semantics, and
base64-encodes the same bytes. A hit anywhere fails the whole export, names the
offending relative path, and writes nothing to `--out`.

The scan scope does not follow the source tier. ADR-0011 §5's rationale is
irreversibility, not the literal presence of a `.git` directory: a
`project_shared` write is hard-refused because "even an instant `git rm` cannot
retract it from any clone or reflog". A user-tier or `project_local` canonical
write is retractable with `rm`; a bundle that has been handed to someone is not.
The tier being private on this machine is precisely what stops being true when
its bytes are packed for transport, so `project_local` — a gitignored draft tier
with no fan-out (ADR-0011 §3) — is not an argument for a weaker export gate but
the case where the gate is doing the most work. Bundles therefore sit on the
git-history side of that line, and `promote_asset` — which scans at
`scope="project_shared"` regardless of anything, because the wiki can be pushed
— is the precedent this follows.

There is consequently **no `--force-unsafe` option on `mm context export`**. The
chokepoint would return `blocked_project_shared` for it anyway; an option that
can only ever be refused is worse than no option, because it advertises a valve
that does not exist.

What this refuses is narrow and worth stating exactly: an artifact with no
secret-shaped content exports from any tier, including to the author's own
second machine. What is refused is packing a *secret-bearing* artifact, from any
tier, for any recipient including oneself. The remediation is the same one
transfer prints — remove the secret, or drop the snapshot that carries it.

**The source walk is descriptor-based and strict.** Enumeration `lstat`s each
entry, and each file is then opened no-follow and non-blocking and verified
with `fstat` on that descriptor, with both the bytes and the `exec` bit taken
from it. Walking with `lstat` and then reading by path would let an external
writer swap a vetted regular file for a symlink or a FIFO between the two,
escaping the artifact or hanging the export while it holds the canonical lock,
and would let `exec` come from a different inode than the content. A symlink
at any path the bundle would otherwise carry — including the artifact root
itself — is refused by name rather than skipped, because a skipped link is a
silent drop the sender never learns about and the mcp-servers copy adapter
already refuses symlinked canonicals for the same
scanned-bytes-equal-promoted-bytes reason. The copier-reserved names (`.git`,
`.DS_Store`, `__pycache__`) and any entry whose name ends in `.bak` — a file or
a whole subtree, matched case-sensitively as the walker does — are excluded at
**every** depth, matching the filtered tree walk the
wiki install and dirty-check paths share, rather than `mm context copy`, whose
`shutil.copytree` carries them. Exclusion is decided **before** the type
check, so an excluded name that happens to be a symlink is skipped as excluded
rather than refused as a link; only a link at a path the bundle would
otherwise have carried is an error. Every exclusion is listed in the export
summary. Enumeration is fail-closed: an unreadable entry aborts the export
instead of shrinking the payload.

**Destination safety.** `--out` is resolved and refused if it lands inside the
source artifact directory: the artifact's own bytes have already been captured
by then, so writing the bundle there would mutate or destroy the thing being
exported after the fact. The bundle is written to a sibling temporary file and
published with a no-replace rename at `0o600`, so an interrupted or failed
export leaves no partial file to block the retry, and export never overwrites an
existing destination or follows a symlink to one. There is no `--overwrite` in
v1.

### 5. Receipt: a bundle is foreign by definition, and every tier is scanned

`mm context import` scans every entry before promoting it, for every destination
tier:

- `project_shared` — hard refusal, no valve. ADR-0011 §5 and ADR-0023 §5 are
  unchanged by this ADR.
- `user` / `project_local` — refused unless `--force-unsafe-import`, the flag
  `mm context init` already uses for the runtime→canonical ingress. The flag is
  threaded into the scan; the chokepoint still hard-refuses `project_shared`
  with it set, so the flag cannot widen the tier above.

This deliberately departs from transfer parity, where Gate A runs **only** for a
`project_shared` destination (`transfer.py`, both the copy and move branches).
The asymmetry is principled: transfer relocates bytes that are already on this
machine and already passed whatever gate admitted them, whereas a bundle is
ingress of foreign bytes. Both existing ingress precedents scan — `mm context
init` with the `--force-unsafe-import` valve (`context/privacy_scan.py` module
docstring), and memory bundle import with its per-record scan of any bundle
whose provenance does not verify.

The cost of following transfer instead is tier-dependent, and worth stating
precisely rather than dramatically. A received secret in the **user** tier would
be caught by the next `mm context sync`, which scans in-memory canonical bytes
per runtime — but as a non-fatal per-runtime skip, so the artifact stays in the
store carrying the secret. In **`project_local`** nothing catches it at all,
because that tier has no fan-out by design (ADR-0011 §3). In both tiers a
`mm context version create` freezes the secret into write-once history first,
and the only hard stop is a later `mm context migrate --to project_shared`,
which re-scans because it delegates to the transfer engine. Scanning at the door
is what keeps that from being the first refusal the user ever sees.

A block leaves zero residue under the destination store: no artifact directory,
no staging directory.

### 6. One receipt sequence, pinned

Everything that can be decided is decided in memory, before the destination
store is touched at all. The order is:

1. **Read and parse the bundle** under the §7 limits: open once
   non-blocking with symlinks refused, `fstat` that descriptor and reject
   anything that is not a regular file, read at most the cap plus one byte from
   it, parse with duplicate object keys refused. The no-follow and non-blocking
   flags are POSIX-only — on Windows they are zero and a reparse point is still
   followed, the same asymmetry the swap-marker reader documents — so the
   guarantee is stated as POSIX-strength, not claimed universally, and the
   `fstat` type check runs on every platform.
2. **Validate the wire form**: the §2 schema, path grammar, and topology;
   strict base64 decode; per-file content digests recomputed from the decoded
   bytes; then `payload_sha256` recomputed over the resulting structure.
3. **Validate the artifact form** entirely from the decoded payload: the
   manifest entry the kind requires is present, the store-name rules hold, and
   the version surface is healthy (§9). The version check reads the payload's
   own `versions.json` bytes and file list, so it needs no materialized tree.
4. **Produce the final payload**: the manifest's frontmatter `name:` is
   rewritten to the landing name (§8). No later step changes a byte.
5. **Scan that payload** entry by entry, fail-fast, per §5.
6. **Take the destination artifact's canonical lock**, run the swap prelude,
   and re-check for a collision inside the lock against **both** identities:
   `lstat` of `<store>/<name>` and of `<store>/<name>.md`. Either one existing
   is a collision, including a dangling symlink or an entry of the wrong type.
7. **Materialize once** into an exclusively created staging directory, from the
   same `bytes` objects the scan judged, creating the `dirs` entries and
   applying `exec` per §2.
8. **Promote**, and remove staging on any failure.

Nothing but the promote happens after materialization, so the only crash window
leaves a staging tree whose bytes were already validated and scanned.

**Staging lives inside the destination store, under a name discovery skips.**
The receipt staging directory is `<store>/.staging-<name>-<pid>-<6 lowercase
hex>.tmp` — the exact grammar the central internal-artifact predicate matches —
created exclusively and never merged into a leftover of the same name, with a
fresh suffix on collision. It must share the destination's parent directory: the
native no-replace rename refuses a cross-parent promote with `EXDEV` by design,
so a staging location outside the store cannot be promoted atomically at all.
That discovery problem is real and is closed here instead of dodged. The skills
lister and the status walk already consult the internal-artifact predicate, but
the **agent and command canonical lister does not** — it accepts any directory
holding its manifest — so an in-store staging tree would be enumerated as an
artifact, and that lister feeds the sync fan-out. This work therefore adds the
predicate check to that lister, pinned by a test, which also closes the same
class for any other internal-shaped leftover.

Enumeration is only half of it: the artifact resolver that answers "is there an
artifact called X" accepts any named directory holding its manifest, so a caller
that already knows a staging directory's name — a web update or delete, or
another transfer's source probe — can address it directly even once listing hides
it, and mutate or remove the tree between the scan and the promote. Guessing the
name means guessing a pid and six random hex characters, and a local writer who
can reach the tree never needed the API, so this is not the primary threat; but
the fix is the same predicate in one more place, so the refusal goes into the
name-addressed resolver as well, and an attempt to update, delete, or transfer an
internal-shaped name is refused rather than served. That closes the window for
this transport and for the transfer engine at the same time, and the pins cover
all three verbs. The transfer engine's
`.migrate-…` staging name is **not** matched by the predicate and so stays
exposed even after that fix; that is a pre-existing transfer-engine defect,
filed with its reproduction as #2304, and this transport avoids it by using the
predicate's own grammar rather than inventing a second one.

Because staging sits inside the store, it inherits the store's own git posture:
a `project_local` landing stages inside `<kind>.local/`, already covered by the
managed `.memtomem/*.local/` ignore rule, and a `project_shared` landing stages
inside a git-tracked directory whose contents cannot contain a secret because
§5 hard-refuses that tier. Receipt still establishes the project-local marker
before taking the lock when landing in `project_local`, failing closed if the
tier cannot be protected, rather than assuming a previous command created it.

**Promotion is no-replace, enforced by the syscall.** Receipt promotes with the
native no-replace rename primitive (`renameat2` / `renamex_np`, with the Windows
rename that already refuses an existing target) rather than an `exists()` check
followed by `os.replace`. The check-then-replace pair the transfer engine uses
cannot see a dangling symlink at the destination and can still replace an entry
created between the two calls; a no-replace rename refuses atomically. Every
refusal it can raise — the existing-target error, a non-empty-directory error,
and a source/target type mismatch — maps to the same typed collision, so a
platform difference cannot turn a refusal into a traceback. `--as <new-name>`
remains the only way to land beside an existing artifact (ADR-0023 §6), and this
ADR adds no overwrite path — which is also what keeps ADR-0022's write-once
guarantee intact, since received `versions/` files are only ever created inside a
directory that did not exist.

Import never re-reads staging in order to scan it. This is stricter than
transfer, which stages and then scans the staged tree, and two hazards make the
in-memory order the right one for ingress: the staged tree lives inside the
destination store during the window, and the tree scanner follows file
symlinks, so a tree
materialized from attacker-supplied entries and then scanned from disk is a
wider surface than one scanned before it exists. The residual external-writer
window between materialize and promote is the one ADR-0023 already accepts; this
ADR does not widen it.

`promote_asset`'s read-once/scan/write-the-same-bytes discipline is the same
rule stated for the export direction.

### 7. Limits and parser bounds

These are part of the format, not an implementation detail, so two
implementations cannot disagree about which bundles are valid:

| Bound | Value |
|---|---|
| Bundle file size | 100 MiB (104857600 bytes), memory-import parity |
| Entry count | 4096 `files`, 4096 `dirs` |
| Total decoded size | 64 MiB (67108864 bytes) |
| Per-file decoded size | 16 MiB (16777216 bytes) |
| Path segment | 64 characters; path total 255 characters, 16 segments |
| JSON nesting depth | 8, counting the root object as depth 1 |

The size cap is enforced on the descriptor, not on a `stat` result: a
`stat`-then-read pair can be defeated by a file that grows or is swapped between
the two, and a FIFO reports a small size and then blocks forever. Open the path
once with symlinks refused, `fstat` the descriptor and refuse anything that is
not a regular file, then read at most the cap plus one byte — a short read is a
valid bundle, a full one is over the cap. Decoded sizes are computed from the
base64 lengths before any decoding, so an inflated bundle is refused without
allocating it. The nesting bound applies to the whole document including the values of
tolerated unknown top-level keys, which is what makes it enforceable at parse
time rather than after the schema check. It counts containers only: the root
object is depth 1, a `files` entry is depth 3, and a scalar leaf adds nothing —
so the schema above sits at depth 3 and a tolerated unknown key may nest five
containers deeper before it is refused.

The decoded `versions.json` is parsed under the **same** rules as the outer
document — duplicate object keys refused, the same nesting bound — rather than
through the version reader's ordinary decoder, whose last-key-wins behavior
would let one bundle yield different tag sets in two implementations. The
version reader is then handed the already-validated structure.

**Bytes become text for scanning exactly one way**, on both export and receipt:
UTF-8 decoded with `errors="replace"`. Strict decoding would refuse a
legitimate binary asset in a skill, and skipping what will not decode would
route those bytes around the gate entirely; replacing undecodable sequences
keeps an ASCII secret embedded in an otherwise-binary file visible to the
scanner. This is the rule the sync-side scan and wiki promotion already use.

Parsing rules: duplicate JSON object keys are refused, which requires the parser
to see the raw member pairs rather than the last-wins mapping a default decoder
produces; base64 is decoded strictly, rejecting characters outside the alphabet
and incorrect padding; every field is type-checked exactly, with no truthiness
coercion and no integer/boolean interchange.

### 8. No field in the bundle is trusted for behavior

| Field | Treatment |
|---|---|
| `format`, `version` | Validated exactly; unknown or newer refused |
| `kind` | Checked against the kind set **and** cross-checked against which manifest filename the entries actually contain |
| `name` | Re-validated; the manifest's frontmatter `name:` is rewritten to the landing name unconditionally, in memory, before the scan; multiple `name:` keys refuse |
| `dirs` | Validated like paths; used only to create empty directories |
| `versions.json` | Content, validated against the entries per §9 |
| `overrides/` | Verbatim content, scanned, and restricted to the known vendor/extension pairs |
| `source.tier`, `source.wiki_commit`, `exported_at` | Informational; rendered only if strictly shaped, never written anywhere |
| `sha256`, `payload_sha256` | Recomputed (§3) |

The name rewrite is unconditional — applied even when no `--as` is passed and
the manifest already agrees — for two different reasons by kind, and it is worth
keeping them apart. For **agents and commands**, sync fans out under the
*parsed* manifest name, so a bundle whose directory says `foo` and whose
manifest says `bar` would fan out as `bar` at the receiver and collide with
whatever owns that name; the rewrite makes the promoted bytes agree with the
promoted path. For **skills**, fan-out uses the directory name, so there is no
collision to prevent; the rewrite is schema consistency, so that a received
skill's manifest does not disagree with its own directory in a way every later
reader has to reconcile.

The rewrite covers the working manifest only, and frozen `versions/vN.md`
snapshots are never rewritten (ADR-0022). For agents and commands that leaves a
real hole, because a labeled sync resolves the snapshot's bytes and fans out
under the name parsed from **them**: a bundle landing as `aaa` could carry a
snapshot declaring `name: victim`, and a later `mm context sync --label v1`
would write `victim`'s runtime target.

ADR-0023 §7 accepts the same mechanism for a renamed local copy — "restoring a
pre-rename version resurrects the old name, which is versioning semantics, not
a transfer bug" — but that precedent does not carry here. A copy's snapshots
are bytes this machine already had; §5 defines a bundle as foreign ingress,
and the same mechanism applied to attacker-chosen bytes is a delayed
name-injection rather than a surprising restore.

So for `agents` and `commands`, **every snapshot's parsed name must equal the
artifact identity**, checked on export against the source name and on receipt
against the landing name, with the offending tag named on refusal. Skills are
unaffected: their fan-out keys on the directory name, so a snapshot cannot
redirect it.

Two details decide what that check actually means, and both are pinned here
because a parser fallback makes them ambiguous. First, a manifest may legally
omit `name:` — commands may carry no frontmatter at all — and the parser then
derives the name from the file path it was handed. A snapshot parsed with its
own path (`versions/v1.md`) would resolve to `v1`, which is nobody's identity,
so snapshots are parsed **as the working manifest they would become**: the
parser is handed the artifact's manifest path and layout, not the snapshot's.
An omitted `name:` therefore resolves to the artifact identity and passes,
which is correct — such a snapshot carries no name to inject. Second, because
that makes an omitted-name snapshot pass under any landing name, the rename
case gets its own rule rather than relying on the parse: **a renamed import
that carries versions is refused outright** for agents and commands, and the
message says the sender must re-export with `--no-versions`, since dropping
history is an export transformation and the receiver has no way to produce a
bundle that never carried it. Relying on the name check alone would refuse the
snapshots that declare a name and quietly relabel the ones that do not.

The name rewrite in the working manifest follows the same principle: an absent
`name:` key is left absent rather than inserted, because the fallback already
yields the landing name and inserting a key would edit a manifest the sender
wrote for no behavioral gain.

### 9. Version state, and the received copy is untracked

A bundle carries a **healthy** version surface or none at all. `--no-versions`
drops `versions/` and `versions.json` together, recorded as `versions_included:
false`, and the choice is echoed in the export summary so the receiver is never
left guessing whether history was withheld.

**`--no-versions` removes the version paths before anything else looks at
them.** They are excluded from the file walk, so they are neither scanned nor
health-checked, and the rules below about a secret in a snapshot or an
unhealthy version surface apply only when versions are being carried. This is
what makes `--no-versions` a usable remedy rather than advice that cannot be
followed: an artifact whose history refuses the export must still be
exportable without that history. A secret inside a frozen snapshot
fails the whole export naming that snapshot — never a per-file strip, which
would hand the receiver a tree the sender believes is complete.

"Healthy" is one validator, run on **both** sides — on export against the source
tree and on receipt against the decoded payload — so a writer can never emit a
bundle its own reader refuses. It goes beyond what the version-manifest reader
checks, because that reader validates syntax, schema, tag shape, and label
names, but not that the manifest agrees with what is on disk. The rules:

- `versions/` and `versions.json` are either both present or both absent — an
  empty directory without a manifest, or a manifest with no directory, is
  refused rather than treated as "no versions";
- every recorded tag has its snapshot at the exact path its layout implies
  (`versions/vN.md` for a file record, `versions/vN/` for a tree record), and no
  tag has both forms;
- the **immediate children** of `versions/` are exactly the set the manifest
  implies — nothing extra, whether a stray file or an orphan `vN/` directory,
  and nothing missing;
- the snapshot layout is one the destination kind can actually resolve: a tree
  snapshot belongs to a skill, and an agent or command carrying one is refused
  rather than landed for a later sync to reject;
- every label targets a tag the manifest records **and** one that kind's label
  path can resolve, so a bundle cannot carry a label the version API itself
  would refuse to create;
- every version's metadata is an object, not a scalar the reader would coerce
  into an empty record;
- for agents and commands, every snapshot parses and names the artifact (§8).

The two manifest-versus-disk directions above are why an **orphan snapshot** —
a `vN.md` written before the manifest entry it belongs to, which is the crash
state the tag allocator is deliberately written to survive — makes export
refuse rather than carry. Carrying it would mean specifying what a receiver
does with an unreferenced snapshot. Refusing on export is also what keeps
`versions_included` honest: there is no state where `versions/` exists without
its manifest.

The refusal is deliberately not framed as "delete the orphan". The version store
keeps an unreferenced snapshot precisely because it may be the only copy of that
history, so the export error names the file and offers the two safe ways
forward — inspect it and repair the manifest, or export with `--no-versions` —
rather than telling the user to remove data the store went out of its way to
preserve.

Version files are never rewritten, including under `--as` (§8, ADR-0022).

**Receipt lands the artifact untracked.** `lock.json` is never written from
bundle-claimed data. A pin asserts that these exact bytes came from a specific
wiki commit, and nothing in a foreign file can establish that about the
receiver's wiki — the ADR-0036 principle that possession of an identifier is not
authorization, applied to a commit SHA. `source.wiki_commit` is populated on
export only when the source classifies as a clean, digest-backed, fully-pinned
install (the ADR-0023 §9 carry gate, reused read-only), and it exists so the
receiver can be told *which* wiki asset to reach for.

The `mm context adopt` follow-up is printed only when the landing name equals
the bundle's name and the destination is `project_shared`. Adopt keys on the
landing name in the receiver's own wiki HEAD and ignores any source commit, so
for a renamed import the hint would point at a different wiki asset entirely —
the same name-key mismatch that makes ADR-0023 §9 refuse a provenance carry on
rename. A renamed import, or one with no `source.wiki_commit`, is told plainly
that it landed untracked.

### 10. CLI only, on both sides

Neither verb gets an MCP tool or a web route.

Import lands bytes into a project from a file the caller names, which is exactly
what [ADR-0008](0008-wiki-layer.md) §"Surface coverage" refuses to expose
headless for `install` / `update`: "exposing them headless would let an agent
snapshot wiki bytes into arbitrary registered projects without the
operator-in-the-loop the dev tier and interactive CLI confirm prompts assume."
A bundle is a weaker provenance than the wiki, so the argument only gets
stronger. Export is an arbitrary-destination write, which is the ADR-0006 Axis G
channel; keeping it off the MCP surface is what §1 relies on.

The existing exact-set assertion over registered context actions pins this: a
future verb has to flip that test deliberately.

`--to` is required on import. The bundle's `source.tier` is untrusted input, and
a foreign file choosing its own landing tier would be a write target chosen by
the thing being written. ADR-0016 §5 does give agents, skills, and commands a v1
default tier, so this is not that ADR forbidding a default — it is this transport
declining to derive one from untrusted input.

**Gate B applies unchanged.** ADR-0011 §5 pairs the explicit tier flag with a
confirmation at the surface, and `--to project_shared` supplies only the first
half. A `project_shared` landing additionally requires
`--confirm-project-shared` — `--yes` alone does not satisfy it, matching every
transfer and migrate surface — and the write records
`project_shared.confirmed_via` in the audit line. The confirmation is evaluated
before any destination mutation, and a dry run never prompts.

**Command surfaces.**

```
mm context export <kind> <name> --out <file> [--from <tier>] [--no-versions]
mm context import <file> --to <tier> [--to-project <selector>] [--as <name>]
                         [--apply] [--yes] [--confirm-project-shared]
                         [--force-unsafe-import]
```

Export resolves its source the way the transfer engine already does, and
adopts that contract rather than restating it: `--from` names the tier
explicitly, and when it is omitted the source tier is auto-detected, with the
same refusal the transfer engine raises when one name resolves in more than
one tier. The project is the current working directory's: export neither
resolves nor mutates another project's canonical store, though `--out` itself
is an operator-named path anywhere on the machine. Import is dry-run by
default and `--apply` performs the write, the family convention every transfer
surface follows.

## Consequences

- One artifact can be handed to a colleague or to another machine as a single
  file, and the receiver's tree matches the sender's under the normalization
  this format defines.
- **What the transport refuses or changes, stated exactly**, since
  "byte-identical" is not true without qualification. Refused loudly, never
  silently dropped: a symlink at any path the bundle would otherwise carry (an
  excluded name is skipped as excluded first, whatever its type); any path
  outside the §2 ASCII allowlist, which includes every non-ASCII filename; a
  case-colliding or ancestor-colliding path pair; an unhealthy version surface,
  including a crash-orphan snapshot; for agents and commands, a version snapshot
  whose manifest name disagrees with the artifact; and any renamed import of an
  agent or command bundle that carries versions at all, which needs a fresh
  export from the sender. Changed on purpose: the working manifest's `name:`
  line is rewritten to the landing name. Excluded rather than carried, and
  listed in the export summary so the omission is visible: `.git`, `.DS_Store`,
  `__pycache__`, and any `*.bak` file or directory at every depth — which means a
  bundle is not identical to what `mm context copy` would produce, since that
  copies them.
  Carried, unlike a naive payload write: empty directories via `dirs`, and the
  executable bit via `exec`, with every other mode bit and every timestamp
  dropped.
- **Non-ASCII artifact filenames cannot be bundled in v1.** This is a real cost
  paid to make the path rules an allowlist with no tail of forgotten refusals.
  Revisit trigger: a report of an artifact that cannot be shared because of it,
  at which point the question to settle is a Unicode normalization and
  collision policy, not a longer denylist.
- A secret-bearing artifact cannot be exported at all, from any tier. This is
  stricter than what the user could do by hand with `tar`, and it is the point:
  the first-party primitive does not become a redistribution path.
- A received artifact is never "installed", so `mm context update` can never
  clobber bytes it did not install. A same-name `project_shared` receipt shows
  as untracked and the result names `mm context adopt` as the follow-up. Every
  other receipt lands without that hint — but adopt keys on the landing name
  against the receiver's own wiki and never reads bundle provenance, so a
  renamed receipt whose new name happens to exist in that wiki can still be
  adopted, and a `user` or `project_local` receipt can reach an adoptable
  state by being migrated to `project_shared` first. The hint is withheld
  where it would mislead, not because the path is closed.
- The bundle reader is a parser over untrusted input and will keep finding new
  input shapes. Its recognized grammar is §§2 and 7–9 plus the
  store-name rules — the wire schema, the path and topology rules, the parser
  bounds, the artifact-form rules (manifest and kind agreement, the multiple
  `name:` refusal, the permitted override pairs), and the version-surface
  rules — declared in the module docstring, and each refused
  shape is pinned as a case in the parametrized reader tests, so the surface is
  bounded by what is written down rather than by what the author happened to
  think of.

## Considered & rejected

- **Reusing `ExportBundle` v2.** Its records are chunk-shaped and its provenance
  key is derived from the memory database path, so an artifact bundle riding it
  would either carry a meaningless key dependency or diverge field-by-field
  until it was a second format wearing the first one's name.
- **Carrying the ADR-0006 F.3 HMAC marker.** §3: no surviving consumer, and it
  imports a fail-closed key dependency into a command that needs no key. The
  `provenance` key is reserved so the decision is reversible.
- **Dropping the executable bit**, on the reasoning that the payload writer
  lands everything at one mode anyway. `mm context copy` copies with metadata
  preserved, so dropping the bit would make a received skill's script
  non-runnable where the equivalent copy kept it. `exec` is carried and bound
  into `payload_sha256` (§2).
- **A denylist of unportable path shapes.** §2: the refusal list has no end, and
  a missed entry lands a file the sender did not describe.
- **A `tar`/`zip` archive instead of JSON.** Archive formats carry symlinks,
  hard links, device nodes, absolute paths, and per-entry modes — every one of
  which this format refuses on purpose — and adopting one means inheriting its
  extraction semantics rather than the store's own payload writer.
- **A networked transport, a share link, or a paste service.** Out of scope per
  the issue, and §1's reasons.
- **A `--force-unsafe` valve on export, for `user` / `project_local` sources.**
  §4. Revisit trigger: a concrete report of the own-machine case — a user
  shipping their own artifact to their own second machine and blocked by a
  secret they accept — at which point the question is whether "the same person
  on another machine" is a distinct destination class, not whether the valve
  should exist for handing files to other people.
- **Not scanning `user` / `project_local` receipts, for transfer parity.** §5.
- **Letting the bundle's `source.tier` default the landing tier.** §10.
- **Staging to disk and then scanning the staged tree, for transfer parity.** §6.
- **Stripping only the offending version snapshot on export.** §9: it produces a
  history the receiver cannot know is incomplete.
- **Carrying an orphan snapshot.** §9: it would require specifying what a
  receiver does with an unreferenced snapshot, and it makes `versions_included`
  ambiguous. The refusal names the file and points at inspection and manifest
  repair, or an export without history — never at deleting it, since the version
  store keeps it precisely because it may be the only copy.
- **Inheriting ADR-0023 §7's rename/version behavior for agents and commands.**
  §8: that precedent is about local bytes; applied to foreign ingress the same
  mechanism is a delayed name injection, so snapshot names must match the
  artifact instead.
- **Rewriting frozen snapshots to the landing name.** It would mutate history to
  fix an identity problem, contradicting ADR-0022; refusing the mismatch keeps
  history immutable and the failure loud.
- **Writing a `lock.json` pin from `source.wiki_commit`.** §9. This is the same
  reasoning ADR-0023 §9 used to gate the transfer carry behind a digest-equality
  check, applied to a case where no such check is possible.
- **An MCP verb or a web route for either side.** §10; the web UI is explicitly
  downstream of this decision per the issue.

## References

- Issue #2298 — this decision's subject. Issue #2297 — the drag-and-drop
  accelerator this was split out of, whose "not in scope" section named the gap.
- Issue #2304 — the transfer engine's `.migrate-…` staging name is not matched
  by the internal-artifact predicate, found while designing §6's staging rules
  and left to its own fix.
- [ADR-0011](0011-canonical-artifact-scope-hierarchy.md) §3 (the `project_local`
  draft tier with no fan-out) and §5 (Gate A at the chokepoint, Gate B at the
  surface, and the no-valve rule for `project_shared`).
- [ADR-0023](0023-cross-project-artifact-transfer.md) — §5 Gate A contract,
  §6 collision policy, §7 copy/rename semantics including frozen snapshots,
  §9 provenance carry gate and its rename refusal.
- [ADR-0022](0022-canonical-artifact-version-snapshots.md) — version snapshots
  as frozen, write-once history.
- [ADR-0030](0030-explicit-preview-pull.md) §10 — skills tree snapshots, the
  swap prelude, and the content-only tree digest.
- [ADR-0008](0008-wiki-layer.md) §"Surface coverage" — no MCP verb for verbs
  that land bytes in a project; lockfile schema.
- [ADR-0006](0006-web-ui-folder-upload-redaction.md) Axis F (bundle provenance
  marker) and Axis G (export/import path authority, an MCP-tool-only question).
- [ADR-0029](0029-mcp-network-transport-auth-stance.md) — no first-party
  network transport auth.
- [ADR-0036](0036-id-addressed-access-scope-vs-namespace.md) — possession of an
  identifier is not authorization.
- Source anchors (grep the symbol if line numbers drift):
  `packages/memtomem/src/memtomem/context/transfer.py`
  (`transfer_artifact` copy branch, `_stage_copy`,
  `_rewrite_staged_manifest_name`, `_classify_provenance_carry`,
  `_sync_followup`, `TransferCollisionError`),
  `packages/memtomem/src/memtomem/wiki/promote.py` (`promote_asset` —
  read-once/scan/commit-same-bytes),
  `packages/memtomem/src/memtomem/context/privacy_scan.py`
  (`scan_text_content`, `raise_or_collect`, `PrivacyBlockedError`),
  `packages/memtomem/src/memtomem/privacy.py` (`enforce_write_guard` —
  `blocked_project_shared`),
  `packages/memtomem/src/memtomem/context/_atomic.py`
  (`_validate_payload_relpath`, `write_tree_payload`, `copy_tree_atomic`),
  `packages/memtomem/src/memtomem/context/versioning.py` (`load_manifest`,
  `_next_version_tag_reconciled` — the orphan-snapshot state),
  `packages/memtomem/src/memtomem/context/_sync_atomic.py` (fan-out keyed on
  the parsed manifest name) and `context/skills.py` (skill fan-out keyed on the
  directory name),
  `packages/memtomem/src/memtomem/context/install.py` (`adopt` — keyed on the
  landing name against the receiver's wiki HEAD),
  `packages/memtomem/src/memtomem/provenance.py` (the marker this ADR does not
  adopt, and its scheme-namespacing rule).
