"""Protect on-disk OAuth credentials from other local users.

Windows needs a protected DACL; os.chmod(..., 0o600) alone does not
meaningfully restrict NTFS access.  Fail closed if permissions cannot be set.
"""
from __future__ import annotations

import functools
import os
import shutil
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
$allowed = @($me.Value, $system.Value)
function Test-PrivateAcl($candidate) {
    if (-not $candidate.AreAccessRulesProtected) { return $false }
    $fullControl = [System.Security.AccessControl.FileSystemRights]::FullControl
    $requiredFlags = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $fullAccess = @{}
    foreach ($rule in $candidate.Access) {
        try {
            $sid = $rule.IdentityReference.Translate(
                [System.Security.Principal.SecurityIdentifier]
            ).Value
        } catch {
            return $false
        }
        if ($sid -notin $allowed -or $rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) {
            return $false
        }
        if (($rule.FileSystemRights -band $fullControl) -ne $fullControl) { continue }
        if ($rule.PropagationFlags -ne [System.Security.AccessControl.PropagationFlags]::None) { continue }
        if ($item.PSIsContainer -and ($rule.InheritanceFlags -band $requiredFlags) -ne $requiredFlags) {
            continue
        }
        $fullAccess[$sid] = $true
    }
    return ($fullAccess.ContainsKey($me.Value) -and $fullAccess.ContainsKey($system.Value))
}
if (Test-PrivateAcl $acl) { exit 0 }

# 只修改 DACL，避免 Set-Acl 写入完整安全描述符时要求额外特权。
# 先保留当前用户和 SYSTEM 的显式授权，再移除继承，避免中途丢失访问权。
if ($item.PSIsContainer) {
    $grantMe = "*${me}:(OI)(CI)F"
    $grantSystem = "*S-1-5-18:(OI)(CI)F"
} else {
    $grantMe = "*${me}:F"
    $grantSystem = "*S-1-5-18:F"
}
& icacls.exe $path /grant:r $grantMe $grantSystem | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'could not grant credential ACL access' }
& icacls.exe $path /inheritance:r | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'could not disable ACL inheritance' }
$acl = Get-Acl -LiteralPath $path
foreach ($rule in @($acl.Access)) {
    try {
        $sid = $rule.IdentityReference.Translate(
            [System.Security.Principal.SecurityIdentifier]
        ).Value
    } catch {
        $sid = $null
    }
    if ($null -eq $sid) { throw 'could not identify credential ACL principal' }
    if ($sid -notin $allowed -or $rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) {
        & icacls.exe $path /remove "*$sid" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'could not remove unexpected credential ACL principal' }
    }
}
& icacls.exe $path /grant:r $grantMe $grantSystem | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'could not grant credential ACL access' }
$actual = Get-Acl -LiteralPath $path
if (-not (Test-PrivateAcl $actual)) { throw 'credential ACL is not private and writable' }
"""


@functools.lru_cache(maxsize=128)
def _protect_windows_file_identity(path: str, file_id: int) -> None:
    del file_id  # Part of the cache key: replacing the file rechecks its ACL.
    env = os.environ.copy()
    env["LLM_GATEWAY_PRIVATE_PATH"] = path
    try:
        result = subprocess.run(
            [(shutil.which("pwsh.exe") or shutil.which("powershell.exe") or "powershell.exe"),
             "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_ACL_SCRIPT],
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PermissionError("could not protect OAuth credential ACL") from exc
    if result.returncode:
        # Do not include PowerShell output: it could reveal local paths.
        raise PermissionError("could not protect OAuth credential ACL")


def _reject_linked_components(path: Path) -> None:
    """Reject symlinks and Windows junctions in the file or its ancestors.

    Inspect the *lexical* path, not Path.resolve(): resolve() would erase
    evidence of a symlink or junction before validation.
    """
    if ".." in path.parts:
        raise PermissionError("OAuth credential path must not contain '..'")
    absolute = path.absolute()  # Absolute, but does not dereference links.
    for component in (absolute, *absolute.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or (
            os.name == "nt"
            and bool(
                getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
        ):
            raise PermissionError("OAuth credential path must not use symlinks or junctions")


def _protect(path: Path, mode: int, *, directory: bool) -> None:
    _reject_linked_components(path)
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
    _reject_linked_components(path)  # Check before mkdir touches a linked parent.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _reject_linked_components(path)  # Recheck newly created directory tree.
    _protect(path.parent, 0o700, directory=True)
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
