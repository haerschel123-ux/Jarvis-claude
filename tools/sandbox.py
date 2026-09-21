"""Filesystem sandbox (Spec §46).

Every path a tool touches passes through here. The check is done on the **fully resolved**
path, so a symlink or an NTFS junction pointing out of a trusted folder is caught rather than
followed.

Four things are refused:

* paths outside the configured trusted folders
* traversal attempts (``..``) that escape a trusted root
* protected operating-system locations, even if a trusted folder would cover them
* network shares (UNC paths) unless explicitly trusted

Nothing here consults model output. The trusted folder list comes only from the user's
settings (Spec §90).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

from core.config import Settings, get_settings
from core.errors import SandboxViolation
from core.logging_setup import get_logger
from core.paths import PATHS

log = get_logger("sandbox")

# Locations that are never writable and never readable by a tool, regardless of settings.
# Reading these has no legitimate use for a personal assistant and leaking them is harmful.
_WINDOWS_PROTECTED = (
    "c:/windows", "c:/program files", "c:/program files (x86)", "c:/programdata/microsoft",
    "c:/$recycle.bin", "c:/system volume information", "c:/boot", "c:/recovery",
)
_POSIX_PROTECTED = (
    "/etc", "/sys", "/proc", "/dev", "/boot", "/root/.ssh", "/var/lib/sudo",
    "/usr/bin", "/usr/sbin", "/sbin", "/bin", "/lib", "/lib64",
)

# Files that hold credentials for JARVIS itself. A tool must never read or write them, or a
# prompt injection could exfiltrate the user's keys through an ordinary "read this file".
_PROTECTED_NAMES = frozenset({"secrets.json", ".env", "id_rsa", "id_ed25519", ".npmrc", ".pgpass"})

MAX_READ_BYTES = 8 * 1024 * 1024


@dataclass(slots=True)
class ResolvedPath:
    """A path that has passed the sandbox check."""

    path: Path
    root: Path
    existed: bool

    @property
    def display(self) -> str:
        return str(self.path)

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "root": str(self.root), "exists": self.existed}


def _normalise(value: str) -> str:
    return str(value).replace("\\", "/").rstrip("/").lower() or "/"


def _is_protected(path: Path) -> str:
    """Return a reason when the path is a protected location, otherwise an empty string."""
    if path.name.lower() in _PROTECTED_NAMES:
        return f"'{path.name}' enthält typischerweise Zugangsdaten und ist gesperrt"

    text = _normalise(str(path))
    protected = _WINDOWS_PROTECTED if sys.platform == "win32" else _POSIX_PROTECTED
    for entry in protected:
        if text == entry or text.startswith(entry + "/"):
            return f"'{entry}' ist ein geschützter Systembereich"

    # JARVIS's own credential storage is off limits to tools on every platform.
    try:
        secrets_dir = _normalise(str(PATHS.data))
        if text.startswith(secrets_dir + "/secrets") or text == _normalise(str(PATHS.secrets_file)):
            return "Der Zugangsdaten-Speicher von JARVIS ist gesperrt"
    except Exception:  # pragma: no cover - defensive
        pass
    return ""


def _is_network_path(raw: str) -> bool:
    text = str(raw).replace("\\", "/")
    return text.startswith("//")


def trusted_roots(settings: Settings | None = None) -> list[Path]:
    """The configured trusted folders, resolved and de-duplicated."""
    settings = settings or get_settings()
    roots: list[Path] = []
    for entry in settings.security.trusted_folders:
        try:
            resolved = Path(entry).expanduser().resolve()
        except (OSError, RuntimeError):
            log.warning("Vertrauenswürdiger Ordner '%s' ist nicht auflösbar", entry)
            continue
        if resolved not in roots:
            roots.append(resolved)
    return roots


def _containing_root(path: Path, roots: list[Path]) -> Path | None:
    for root in roots:
        try:
            path.relative_to(root)
        except ValueError:
            continue
        return root
    return None


def resolve_path(
    raw: str,
    *,
    settings: Settings | None = None,
    must_exist: bool = False,
    for_write: bool = False,
) -> ResolvedPath:
    """Validate and resolve a path, or raise :class:`SandboxViolation`.

    ``for_write`` only changes the error wording; the trust check itself is identical, because
    reading a file outside the trusted folders is an exfiltration risk just as writing outside
    them is a damage risk.
    """
    settings = settings or get_settings()
    if not raw or not str(raw).strip():
        raise SandboxViolation("Empty path", user_message="Es wurde kein Pfad angegeben.")

    text = str(raw).strip().strip('"')

    if _is_network_path(text):
        raise SandboxViolation(
            f"Network path rejected: {text}",
            user_message="Netzwerkfreigaben sind gesperrt.",
        )

    if "\x00" in text:
        raise SandboxViolation("Null byte in path", user_message="Ungültiger Pfad.")

    candidate = Path(os.path.expandvars(text)).expanduser()

    roots = trusted_roots(settings)
    if not roots:
        raise SandboxViolation(
            "No trusted folders configured",
            user_message=(
                "Es ist noch kein vertrauenswürdiger Ordner freigegeben. Lege in den "
                "Einstellungen unter 'Sicherheit' fest, auf welche Ordner ich zugreifen darf."
            ),
        )

    if not candidate.is_absolute():
        # A relative path is interpreted inside the first trusted folder rather than the
        # process working directory, which the user has no view of.
        candidate = roots[0] / candidate

    # Resolve symlinks and "..", which is what makes junction and traversal escapes visible.
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        raise SandboxViolation(
            f"Path cannot be resolved: {text}",
            user_message=f"Der Pfad konnte nicht aufgelöst werden: {exc}",
        ) from exc

    if reason := _is_protected(resolved):
        raise SandboxViolation(
            f"Protected location: {resolved}",
            user_message=f"Zugriff verweigert — {reason}.",
        )

    root = _containing_root(resolved, roots)
    if root is None:
        action = "beschreiben" if for_write else "lesen"
        raise SandboxViolation(
            f"Path outside trusted folders: {resolved}",
            detail={"path": str(resolved), "roots": [str(r) for r in roots]},
            user_message=(
                f"Ich darf '{resolved}' nicht {action}. Der Pfad liegt außerhalb der "
                "freigegebenen Ordner: " + ", ".join(str(r) for r in roots)
            ),
        )

    # The parent of a not-yet-existing file must also pass, so a write cannot create a file
    # through a symlinked parent that points outside.
    if not resolved.exists():
        parent = resolved.parent
        try:
            resolved_parent = parent.resolve()
        except (OSError, RuntimeError):
            resolved_parent = parent
        if _containing_root(resolved_parent, roots) is None:
            raise SandboxViolation(
                f"Parent directory outside trusted folders: {resolved_parent}",
                user_message=f"Das Zielverzeichnis '{resolved_parent}' ist nicht freigegeben.",
            )

    if must_exist and not resolved.exists():
        raise SandboxViolation(
            f"Path does not exist: {resolved}",
            user_message=f"'{resolved}' existiert nicht.",
        )

    return ResolvedPath(resolved, root, resolved.exists())


def is_trusted(raw: str, settings: Settings | None = None) -> bool:
    """Non-raising variant, for UI hints."""
    try:
        resolve_path(raw, settings=settings)
    except SandboxViolation:
        return False
    return True


def scope_for(path: Path | str) -> str:
    """The scope key used by 'always allow for this folder' (Spec §69)."""
    candidate = PurePath(str(path))
    return str(candidate.parent if candidate.suffix else candidate)


def describe_sandbox(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    roots = trusted_roots(settings)
    return {
        "trusted_folders": [str(r) for r in roots],
        "configured": bool(roots),
        "protected_locations": list(
            _WINDOWS_PROTECTED if sys.platform == "win32" else _POSIX_PROTECTED
        ),
        "protected_filenames": sorted(_PROTECTED_NAMES),
        "max_read_bytes": MAX_READ_BYTES,
    }
