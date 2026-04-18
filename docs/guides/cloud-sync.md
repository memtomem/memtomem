# Cloud Sync Client Setup

memtomem does not call any cloud provider's API. It reads files from the
local filesystem only — the sync client (Google Drive for desktop, OneDrive,
iCloud Drive, and similar) handles all authentication and mirrors the cloud
to a local path. This guide walks through installing and configuring that
client so the shared folder appears where
`MEMTOMEM_INDEXING__MEMORY_DIRS` can point at it.

Once the client is working, continue with
[Google Drive Integration (Multi-Device / Team)](google-drive.md).

## Common concepts

### No memtomem-side authentication

memtomem never talks to Google Drive / OneDrive / iCloud APIs. No OAuth, no
API tokens. The only sign-in step is the sync client's own — memtomem just
reads files the client has placed on disk.

### Always-offline vs streaming

Every desktop sync client offers two flavors of on-disk behavior. memtomem
needs files to actually be on disk:

| Mode | What it does | memtomem |
|------|--------------|----------|
| Always on disk ("Mirror", "Files On-Demand OFF", etc.) | Full copies live locally; the client keeps them in sync | **Works out of the box** |
| On-demand ("Stream", "Files On-Demand ON", iCloud "Optimize Mac Storage") | Files may be cloud-only placeholders until opened | **Pin the folder offline first** — the indexer cannot read placeholders |

Pin the `memtomem-memories` folder as always-available-offline regardless of
the global mode. Per-provider instructions below.

### Never put the SQLite DB on a synced folder

Sync clients race on `.db`, `.db-wal`, and `.db-shm` files — copying them at
different moments causes partial writes that corrupt the database (see
[sqlite.org/howtocorrupt.html](https://www.sqlite.org/howtocorrupt.html)).
Keep `MEMTOMEM_STORAGE__SQLITE_PATH` on a local-only path such as
`~/.memtomem/memtomem.db`. Only the markdown files belong on the sync
folder.

## Google Drive for desktop

1. **Install** — download from [google.com/drive/download](https://www.google.com/drive/download/) (macOS / Windows). Sign in with Google on first launch. Work/school accounts may require an admin to pre-authorize the installer.
2. **Pick a mode** — Preferences → Google Drive tab → "My Drive syncing options":
   - **Mirror** — full copies on disk. Recommended for memtomem.
   - **Stream** — files appear as a mounted location, downloaded on access. If you use Stream, right-click `memtomem-memories` in Finder / Explorer → **"Make available offline"** to pin it.
3. **Local path**:
   - macOS (12.3+): `~/Library/CloudStorage/GoogleDrive-{email}/My Drive/`
   - Windows: usually `G:\My Drive\` — the drive letter is configurable in Preferences.
4. **Verify** — at [drive.google.com](https://drive.google.com), create `memtomem-memories/test.md`. Wait a few seconds for sync, then in a terminal:
   ```bash
   ls ~/Library/CloudStorage/GoogleDrive-*/My\ Drive/memtomem-memories/
   # expect: test.md
   ```

## OneDrive

1. **Install** — preinstalled on Windows 10 / 11. On macOS, download from [onedrive.live.com](https://onedrive.live.com/) (Personal) or the Microsoft 365 portal (Business / School). Sign in.
2. **Files On-Demand** — Settings → Sync and Backup → Advanced Settings:
   - **OFF** — every file lives on disk (equivalent to Mirror). Recommended for memtomem.
   - **ON** — files can be cloud-only. If you leave it ON, right-click `memtomem-memories` → **"Always keep on this device"** to pin.
3. **Local path**:
   - Windows: `%USERPROFILE%\OneDrive` (Personal) or `%USERPROFILE%\OneDrive - {TenantName}` (Business).
   - macOS: `~/Library/CloudStorage/OneDrive-Personal/` (Personal). The Business form is `~/Library/CloudStorage/OneDrive-{Tenant}/`, but the tenant suffix varies — run `ls ~/Library/CloudStorage/` after sign-in to find the exact folder name.
4. **Verify** — create `memtomem-memories/test.md` in the OneDrive web UI, then `ls` the local path above.

## iCloud Drive

Best supported on macOS and iOS. The iCloud for Windows client exists but
has fewer capabilities, and there is no Linux client — iCloud Drive is most
useful for single-platform Mac setups.

1. **Enable** — System Settings → Apple Account → iCloud → iCloud Drive → **On**.
2. **Turn OFF "Optimize Mac Storage"** — System Settings → iCloud → iCloud Drive → uncheck *Optimize Mac Storage*. When enabled, macOS evicts file contents to save space and leaves placeholders the memtomem indexer cannot read.
3. **Local path**: `~/Library/Mobile Documents/com~apple~CloudDocs/`. Create `memtomem-memories/` inside that folder.
4. **Verify** — on iCloud.com or another Apple device, add `test.md` to `memtomem-memories/`, then on the Mac:
   ```bash
   ls ~/Library/Mobile\ Documents/com~apple~CloudDocs/memtomem-memories/
   # expect: test.md
   ```

## Next step

With a verified local path in hand, use it as
`MEMTOMEM_INDEXING__MEMORY_DIRS[0]` and follow the rest of
[Google Drive Integration (Multi-Device / Team)](google-drive.md) — folder
structure, namespaces, `mem_index`, and team workflow. The Google-Drive
examples there apply identically to OneDrive and iCloud Drive once you
substitute the local path above.
