# Refreshing AnyIO in existing memtomem environments

The repository lockfile uses AnyIO 4.14.2, which fixes
[GHSA-82r6-8w77-94w6](https://osv.dev/vulnerability/GHSA-82r6-8w77-94w6)
and [GHSA-5p39-cfhj-2xmp](https://osv.dev/vulnerability/GHSA-5p39-cfhj-2xmp).
A lockfile update does not change an already-installed runtime.

The TLS advisory concerns internationalized (non-ASCII) hostnames when a
connection has also been hijacked. This is a preventive dependency update,
not a claim that every memtomem installation is exploitable. No AnyIO
process-pool call was found in memtomem or the inspected server dependencies.

Before updating any installation, close MCP clients and stop all memtomem
processes, including background Web UI servers. Restart them afterward;
changing packages on disk does not update code already loaded in a process.

## Installed with uv tool

Use the supported upgrade workflow:

```bash
mm upgrade
```

It detects the original extras and manages server/Web UI process cleanup on
POSIX. On Windows, stop those processes manually and follow its restart
warnings. Review the detected extras; use `--extras` if auto-detection fails.
See the [upgrade reference](reference/data-config-cli.md) for details.

If `mm` is unavailable, stop the MCP servers and background Web UI manually,
then use `uv tool upgrade memtomem`. This updates dependencies while retaining
the original installation constraints and extras. If custom constraints keep
AnyIO below 4.14.2, resolve them first. Inspect the tool environment beneath
`uv tool dir` with its Python interpreter and the version probe below.

## Installed with pipx

After stopping running memtomem processes, update the dependency in pipx's
managed environment and check the reported version:

```bash
pipx runpip memtomem install --upgrade 'anyio>=4.14.2,<5'
pipx runpip memtomem show anyio
```

Confirm a version of at least 4.14.2, then restart the clients and Web UI.

## Cached uvx launches, including plugins

Keep the existing MCP registration, package version, extras, and launch command.
Do not add a parallel manual registration for a plugin-managed server.

While online, close clients using uvx tools and run this one-time cache refresh:

```bash
uv cache clean anyio
uv cache prune
```

The first command clears cached AnyIO package/index data; it alone does not
reliably replace an existing uvx environment. The second removes cached tool
environments, so the original launch command resolves its dependencies again.
It also removes other disposable uvx environments and dangling cache entries;
those tools may need network access on their next launch. Installed `uv tool`
environments are separate: use the uv tool section above to update them, including
when uvx reuses an installed tool. Do not use `--force` with active tools.

Restart the original client while online. No refresh flag belongs in the
persistent MCP configuration. Custom dependency pins or private indexes must
provide AnyIO >=4.14.2. For the default plugin's package specification, inspect
the same resolved environment with:

```bash
uvx --from 'memtomem[onnx]==0.6.5' python -c "from importlib.metadata import version; print(version('anyio'))"
```

For a different launcher, substitute its exact `--from` value and preserve any
existing resolution options. Confirm a version of at least 4.14.2. See the
[plugin registration guidance](integrations/claude-code.md#option-a-install-the-safe-base-plugin)
for avoiding duplicate MCP servers.

## Installed in a virtual environment

Activate the environment that actually runs memtomem, then run:

```bash
uv pip install --upgrade 'anyio>=4.14.2,<5'
python -c "from importlib.metadata import version; print(version('anyio'))"
```

Restart running memtomem processes. For a project managed by its own lockfile,
update that lockfile as well so the next sync does not restore the old version.

The environment distinctions follow the [uv tools documentation](https://docs.astral.sh/uv/concepts/tools/).
