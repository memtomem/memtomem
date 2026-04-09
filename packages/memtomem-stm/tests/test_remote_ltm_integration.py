"""End-to-end: SurfacingEngine talks to a remote MCP server via stdio.

After the move to remote-only LTM access, STM's surfacing engine reaches the
LTM exclusively through `McpClientSearchAdapter`, which spawns (or connects
to) a memtomem MCP server over stdio. This test exercises that path against a
tiny fake MCP server (_fake_memtomem_server.py) so the integration runs in
under a second and doesn't require memtomem core to be installed.

What this proves:
1. McpClientSearchAdapter can spawn and initialise a child stdio MCP process.
2. SurfacingEngine routes search through the adapter only — there is no
   in-process SearchPipeline fallback path anymore.
3. The canned hits returned by the child server flow back through the
   adapter, the surfacing pipeline, and into the injected response.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.engine import SurfacingEngine
from memtomem_stm.surfacing.mcp_client import McpClientSearchAdapter

_FAKE_SERVER = Path(__file__).parent / "_fake_memtomem_server.py"

LONG_RESPONSE = "x" * 200
VALID_ARGS = {"path": "src/auth.py", "_context_query": "JWT authentication"}


@pytest.mark.asyncio
async def test_surfacing_via_remote_stdio_mcp_server():
    config = SurfacingConfig(
        enabled=True,
        min_response_chars=10,
        cooldown_seconds=0,
        max_surfacings_per_minute=1000,
        auto_tune_enabled=False,
        include_session_context=False,
        fire_webhook=False,
        feedback_enabled=False,
        ltm_mcp_command=sys.executable,
        ltm_mcp_args=[str(_FAKE_SERVER)],
    )

    adapter = McpClientSearchAdapter(config)
    await adapter.start()
    try:
        engine = SurfacingEngine(config, mcp_adapter=adapter)
        output = await engine.surface("gh", "read_file", VALID_ARGS, LONG_RESPONSE)
    finally:
        await adapter.stop()

    assert "Relevant Memories" in output, output
    # Canned content from the fake server should land in the surfaced response
    assert "JWT authentication" in output
