from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import time

from aiohttp import ClientSession

import pytest

from llm_gateway.chatgpt_auth import (
    _candidate_auth_files,
    _read_auth_file,
    _write_auth_file,
    get_chatgpt_credentials,
)
from llm_gateway.credential_security import write_private_auth_file


def test_auth_write_and_refresh_use_private_atomic_files(tmp_path):
    path = tmp_path / "private" / "auth.json"
    _write_auth_file(path, {"access_token": "first", "refresh_token": "secret"})
    assert json.loads(path.read_text())["access_token"] == "first"
    assert not list(path.parent.glob(".auth.json.tmp-*"))
    _write_auth_file(path, {"access_token": "second"})
    assert _read_auth_file(path) == {"access_token": "second"}
    assert not list(path.parent.glob(".auth.json.tmp-*"))
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_read_hardens_preexisting_world_readable_credentials(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text('{"access_token": "old"}')
    path.chmod(0o644)
    assert _read_auth_file(path) == {"access_token": "old"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_auth_file_symlink_is_refused(tmp_path):
    actual = tmp_path / "real.json"
    actual.write_text('{"access_token": "old"}')
    link = tmp_path / "auth.json"
    try:
        link.symlink_to(actual)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(PermissionError):
        _read_auth_file(link)
    with pytest.raises(PermissionError):
        _write_auth_file(link, {"access_token": "new"})
    assert json.loads(actual.read_text())["access_token"] == "old"


def test_no_sensitive_temporary_file_left_when_replace_fails(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"

    def denied(_source, _destination):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", denied)
    with pytest.raises(OSError):
        write_private_auth_file(path, '{"refresh_token": "secret"}')
    assert not path.exists()
    assert not list(tmp_path.glob(".auth.json.tmp-*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows ACLs")
def test_windows_token_acl_has_no_extra_principals(tmp_path):
    path = tmp_path / "auth.json"
    _write_auth_file(path, {"access_token": "secret"})
    # Verify separately from the helper, through the OS, on Windows CI.
    script = (
        "$a=Get-Acl -LiteralPath $env:LLM_GATEWAY_TEST_AUTH;"
        "if(-not $a.AreAccessRulesProtected){exit 1};"
        "$me=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;"
        "foreach($r in $a.Access){"
        "$id=$r.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value;"
        "if($id -notin @($me,'S-1-5-18')){exit 2}"
        "}"
    )
    env = os.environ.copy()
    env["LLM_GATEWAY_TEST_AUTH"] = str(path)
    subprocess.run(
        [(shutil.which("pwsh.exe") or shutil.which("powershell.exe") or "powershell.exe"),
         "-NoProfile", "-NonInteractive", "-Command", script],
        env=env,
        check=True,
    )


def _create_synthetic_auth(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"access_token": "synthetic-test-token", "expires_at": time.time() + 3600}),
        encoding="utf-8",
    )


def _assert_configured_path_is_rejected(token_dir, auth_name="auth.json"):
    environ = {"CHATGPT_TOKEN_DIR": str(token_dir), "CHATGPT_AUTH_FILE": auth_name}
    candidate = _candidate_auth_files(environ, None)[0]
    assert candidate == token_dir / auth_name

    async def exercise():
        async with ClientSession() as session:
            await get_chatgpt_credentials(
                session,
                environ=environ,
                allow_device_login=False,
            )

    with pytest.raises(PermissionError, match="symlinks or junctions"):
        asyncio.run(exercise())
    with pytest.raises(PermissionError, match="symlinks or junctions"):
        _write_auth_file(candidate, {"access_token": "overwritten"})


def test_config_entry_preserves_file_symlink_and_rejects_read_write(tmp_path):
    real = tmp_path / "real.json"
    _create_synthetic_auth(real)
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    link = token_dir / "auth.json"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks are not permitted on this host")
    _assert_configured_path_is_rejected(token_dir)
    assert json.loads(real.read_text())["access_token"] == "synthetic-test-token"


def test_config_entry_rejects_linked_directory_component(tmp_path):
    actual = tmp_path / "actual"
    _create_synthetic_auth(actual / "auth.json")
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(actual, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are not permitted on this host")
    _assert_configured_path_is_rejected(linked)
    assert json.loads((actual / "auth.json").read_text())["access_token"] == "synthetic-test-token"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction behavior")
def test_config_entry_rejects_windows_junction(tmp_path):
    actual = tmp_path / "real_tokens"
    _create_synthetic_auth(actual / "auth.json")
    junction = tmp_path / "junction_tokens"
    creation = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(actual)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert creation.returncode == 0, "Windows CI must be able to create a junction"
    # This is the exact configuration entry affected by the resolve() bypass.
    _assert_configured_path_is_rejected(junction)
    assert json.loads((actual / "auth.json").read_text())["access_token"] == "synthetic-test-token"


def test_normal_config_entry_still_reads_credentials(tmp_path):
    token_dir = tmp_path / "ordinary_tokens"
    auth_file = token_dir / "auth.json"
    _create_synthetic_auth(auth_file)
    environ = {"CHATGPT_TOKEN_DIR": str(token_dir)}

    async def exercise():
        async with ClientSession() as session:
            return await get_chatgpt_credentials(
                session,
                environ=environ,
                allow_device_login=False,
            )

    credentials = asyncio.run(exercise())
    assert credentials.access_token == "synthetic-test-token"


def test_parent_traversal_is_not_accepted_as_credentials_path(tmp_path):
    # Do not normalize ".." before inspecting the original components.
    candidate = tmp_path / "tokens" / ".." / "real.json"
    with pytest.raises(PermissionError, match="must not contain"):
        _read_auth_file(candidate)
    with pytest.raises(PermissionError, match="must not contain"):
        _write_auth_file(candidate, {"access_token": "nope"})
