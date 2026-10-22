"""Protect on-disk OAuth credentials from other local users.

Windows needs a protected DACL; os.chmod(..., 0o600) alone does not
meaningfully restrict NTFS access.  Fail closed if permissions cannot be set.
"""
from __future__ import annotations

import functools
import os
import stat
import subprocess
import tempfile
from pathlib import Path


_WINDOWS_ACL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$path = $env:LLM_GATEWAY_PRIVATE_PATH
if ([string]::IsNullOrWhiteSpace($path)) { throw 'missing credential path' }
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$system = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$item = Get-Item -LiteralPath $path -Force
$acl = Get-Acl -LiteralPath $path
$acl.SetAccessRuleProtection($true, $false)
foreach ($rule in @($acl.Access)) {
    [void]$acl.RemoveAccessRuleSpecific($rule)
}
if ($item.PSIsContainer) {
    $flags = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
} else {
    $flags = [System.Security.AccessControl.InheritanceFlags]::None
}
foreach ($sid in @($me, $system)) {
    $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
        $sid,
        [System.Security.AccessControl.FileSystemRights]::FullControl,
        $flags,
        [System.Security.AccessControl.PropagationFlags]::None,
        [System.Security.AccessControl.AccessControlType]::Allow
    )
    [void]$acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $path -AclObject $acl
$actual = Get-Acl -LiteralPath $path
if (-not $actual.AreAccessRulesProtected) { throw 'ACL inheritance still enabled' }
$allowed = @($me.Value, $system.Value)
foreach ($rule in $actual.Access) {
    $sid = $rule.IdentityReference.Translate(
        [System.Security.Principal.SecurityIdentifier]
    ).Value
    if ($sid -notin $allowed) { throw 'unexpected principal can access credential' }
}
"""


@functools.lru_cache(maxsize=128)
def _protect_windows_file_identity(path: str, file_id: int) -> None:
    del file_id  # Part of the cache key: replacing the file rechecks its ACL.
    env = os.environ.copy()
    env["LLM_GATEWAY_PRIVATE_PATH"] = path
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_ACL_SCRIPT],
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PermissionError("could not protect OAuth credential ACL") from exc
    if result.returncode:
        # Do not include PowerShell stdout/stderr: they may reveal local paths.
        raise PermissionError("could not protect OAuth credential ACL")


def _protect(path: Path, mode: int, *, directory: bool) -> None:
    if path.is_symlink():
        raise PermissionError("OAuth credential path must not be a symlink")
    info = path.stat()
    if directory and not stat.S_ISDIR(info.st_mode):
        raise PermissionError("OAuth credential directory is not a directory")
    if not directory and not stat.S_ISREG(info.st_mode):
        raise PermissionError("OAuth credential file is not a regular file")
    if os.name == "nt":
        _protect_windows_file_identity(str(path), info.st_ino)
    else:
        os.chmod(path, mode)


def protect_existing_auth_file(path: Path) -> None:
    """Secure even previously created OAuth files before reading them."""
    try:
        _protect(path, 0o600, directory=False)
    except FileNotFoundError:
        return


def write_private_auth_file(path: Path, text: str) -> None:
    """Create a protected temporary file, then atomically replace the token."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _protect(path.parent, 0o700, directory=True)
    if path.is_symlink():
        raise PermissionError("OAuth credential path must not be a symlink")
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    temporary = Path(name)
    try:
        os.close(fd)
        _protect(temporary, 0o600, directory=False)
        with temporary.open("w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        # The temp file's restrictive ACL travels with it on replace.
        _protect(path, 0o600, directory=False)
    finally:
        temporary.unlink(missing_ok=True)
