from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess

import pytest

from llm_gateway.chatgpt_auth import _read_auth_file, _write_auth_file
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
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env=env,
        check=True,
    )
