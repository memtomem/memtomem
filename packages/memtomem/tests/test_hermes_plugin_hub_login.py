"""The Hermes plugin's launch env keeps the user's Hugging Face credentials off Hub requests.

Hermes's plugin catalog does not list a plugin that reads another tool's login, and
huggingface-hub attaches the login ``hf auth login`` saved to every request that names no
token. The plugin's server entry therefore sets ``HF_HUB_DISABLE_IMPLICIT_TOKEN``.

Each case starts a child interpreter, because huggingface-hub reads its environment at
import. The child gets the env rendered into the plugin's ``mcp.json``, a temporary home
holding a saved login, and nothing of the parent's Hugging Face or XDG settings, so a
login or ``HF_TOKEN`` on the machine running the tests cannot decide a case.

This pins header construction with the huggingface-hub installed here. Two things it does
not show are measured against a launched server instead: that the launch line's ``--with``
floor makes uv resolve a huggingface-hub new enough to skip the read (releases before
1.20.0 open the saved login even with the flag set), and what the HTTP transport adds.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("huggingface_hub")

_ROOT = Path(__file__).resolve().parents[3]
_FLAG = "HF_HUB_DISABLE_IMPLICIT_TOKEN"
# Assembled at run time so no token-shaped literal sits in the repository.
_SAVED_LOGIN = "hf_" + "saved" * 7
_ENV_TOKEN = "hf_" + "fromenv" * 5
# What an interpreter needs to start, none of which huggingface-hub reads for credentials.
_PASSED_THROUGH = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")

_CHILD = """
import json
try:
    from huggingface_hub.utils import build_hf_headers
    headers = {key.lower(): value for key, value in build_hf_headers().items()}
    print(json.dumps({"authorization": headers.get("authorization")}))
except Exception as exc:
    print(json.dumps({"raised": type(exc).__name__}))
"""


def _rendered_env() -> dict[str, str]:
    path = _ROOT / "packages/memtomem-hermes-plugin/mcp.json"
    rendered = json.loads(path.read_text(encoding="utf-8"))
    return dict(rendered["mcpServers"]["memtomem"]["env"])


def _hub_headers(
    home: Path, *, flag: bool, env_token: bool = False, readable: bool = True
) -> dict[str, str | None]:
    """What a child with the plugin's launch env reports for its default Hub headers."""
    saved = home / ".cache" / "huggingface" / "token"
    saved.parent.mkdir(parents=True)
    saved.write_text(_SAVED_LOGIN, encoding="utf-8")

    env = _rendered_env()
    assert env[_FLAG] == "1"  # the cases below are about this key; a rename must fail here
    if not flag:
        del env[_FLAG]
    if env_token:
        env["HF_TOKEN"] = _ENV_TOKEN
    env.update({"HOME": str(home), "USERPROFILE": str(home)})
    env.update({key: os.environ[key] for key in _PASSED_THROUGH if key in os.environ})

    if not readable:
        saved.chmod(0o000)
    try:
        if not readable and os.access(saved, os.R_OK):
            pytest.skip("mode bits do not deny this user the file")
        done = subprocess.run(
            [sys.executable, "-c", _CHILD],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
    finally:
        saved.chmod(0o600)
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_launch_env_sends_no_saved_login(tmp_path: Path) -> None:
    assert _hub_headers(tmp_path, flag=True) == {"authorization": None}


def test_the_launch_env_drops_hf_token_too(tmp_path: Path) -> None:
    # The flag turns off every implicit source, the environment variable included; the
    # README sends a user who needs a token to their own entry without the flag.
    assert _hub_headers(tmp_path, flag=True, env_token=True) == {"authorization": None}


def test_without_the_flag_the_saved_login_is_sent(tmp_path: Path) -> None:
    # The control: the same child finds the saved login, so the cases above can fail.
    assert _hub_headers(tmp_path, flag=False) == {"authorization": f"Bearer {_SAVED_LOGIN}"}


def test_without_the_flag_hf_token_wins_over_the_saved_login(tmp_path: Path) -> None:
    sent = _hub_headers(tmp_path, flag=False, env_token=True)
    assert sent == {"authorization": f"Bearer {_ENV_TOKEN}"}


def test_the_launch_env_does_not_open_the_saved_login(tmp_path: Path) -> None:
    # huggingface-hub catches only a missing token file, so opening one it may not read
    # raises. Passing here means the file was not opened.
    assert _hub_headers(tmp_path, flag=True, readable=False) == {"authorization": None}


def test_without_the_flag_the_saved_login_is_opened(tmp_path: Path) -> None:
    # The control for the unreadable file: without the flag the same child does open it.
    assert _hub_headers(tmp_path, flag=False, readable=False) == {"raised": "PermissionError"}
