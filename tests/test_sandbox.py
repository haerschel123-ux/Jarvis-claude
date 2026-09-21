"""Path sandbox: every escape route must be closed (Spec §46, §90)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from core.config import Settings
from core.errors import SandboxViolation
from tools.sandbox import is_trusted, resolve_path, scope_for


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, Settings]:
    """A trusted 'projects' folder next to an untrusted 'outside' folder."""
    trusted = tmp_path / "projects"
    outside = tmp_path / "outside"
    trusted.mkdir()
    outside.mkdir()
    (trusted / "notes.txt").write_text("ok", encoding="utf-8")
    (outside / "secret.txt").write_text("private", encoding="utf-8")
    settings = Settings()
    settings.security.trusted_folders = [str(trusted)]
    return tmp_path, settings


def test_file_inside_a_trusted_folder_is_allowed(workspace) -> None:
    base, settings = workspace
    resolved = resolve_path(str(base / "projects" / "notes.txt"), settings=settings)
    assert resolved.path == (base / "projects" / "notes.txt").resolve()
    assert resolved.existed is True


def test_traversal_out_of_a_trusted_folder_is_blocked(workspace) -> None:
    base, settings = workspace
    with pytest.raises(SandboxViolation, match="outside trusted folders"):
        resolve_path(str(base / "projects" / ".." / "outside" / "secret.txt"), settings=settings)


def test_path_outside_the_trusted_folders_is_blocked(workspace) -> None:
    base, settings = workspace
    with pytest.raises(SandboxViolation):
        resolve_path(str(base / "outside" / "secret.txt"), settings=settings)


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges on Windows")
def test_symlink_escape_is_blocked(workspace) -> None:
    """A link inside a trusted folder pointing out of it must not grant access."""
    base, settings = workspace
    os.symlink(base / "outside", base / "projects" / "escape")
    with pytest.raises(SandboxViolation):
        resolve_path(str(base / "projects" / "escape" / "secret.txt"), settings=settings)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths")
def test_protected_system_locations_are_blocked(workspace) -> None:
    _, settings = workspace
    for path in ("/etc/passwd", "/proc/self/environ", "/boot/grub"):
        with pytest.raises(SandboxViolation) as info:
            resolve_path(path, settings=settings)
        assert "geschützter Systembereich" in info.value.user_message


def test_a_trusted_folder_cannot_unlock_a_protected_location(tmp_path: Path) -> None:
    """Even trusting '/' must not expose /etc — protected locations are absolute."""
    settings = Settings()
    settings.security.trusted_folders = ["/"]
    if sys.platform != "win32":
        with pytest.raises(SandboxViolation):
            resolve_path("/etc/shadow", settings=settings)


def test_credential_files_are_blocked_even_inside_a_trusted_folder(workspace) -> None:
    """A prompt injection must not be able to read the user's keys through read_file."""
    base, settings = workspace
    (base / "projects" / ".env").write_text("OPENROUTER_API_KEY=secret", encoding="utf-8")
    with pytest.raises(SandboxViolation) as info:
        resolve_path(str(base / "projects" / ".env"), settings=settings)
    assert "Zugangsdaten" in info.value.user_message


def test_network_shares_are_blocked(workspace) -> None:
    _, settings = workspace
    with pytest.raises(SandboxViolation) as info:
        resolve_path("//fileserver/share/data.xml", settings=settings)
    assert "Netzwerkfreigaben" in info.value.user_message


def test_nothing_is_reachable_without_configured_trusted_folders(tmp_path: Path) -> None:
    settings = Settings()
    settings.security.trusted_folders = []
    with pytest.raises(SandboxViolation) as info:
        resolve_path(str(tmp_path / "anything.txt"), settings=settings)
    assert "vertrauenswürdiger Ordner" in info.value.user_message


def test_relative_paths_resolve_inside_the_first_trusted_folder(workspace) -> None:
    base, settings = workspace
    resolved = resolve_path("notes.txt", settings=settings)
    assert resolved.path == (base / "projects" / "notes.txt").resolve()


def test_a_new_file_is_allowed_but_not_through_an_escaping_parent(workspace) -> None:
    base, settings = workspace
    assert resolve_path(str(base / "projects" / "new.txt"), settings=settings).existed is False
    with pytest.raises(SandboxViolation):
        resolve_path(str(base / "outside" / "new.txt"), settings=settings, for_write=True)


def test_must_exist_is_enforced(workspace) -> None:
    base, settings = workspace
    with pytest.raises(SandboxViolation) as info:
        resolve_path(str(base / "projects" / "ghost.txt"), settings=settings, must_exist=True)
    assert "existiert nicht" in info.value.user_message


def test_empty_and_null_byte_paths_are_rejected(workspace) -> None:
    _, settings = workspace
    for value in ("", "   ", "a\x00b"):
        with pytest.raises(SandboxViolation):
            resolve_path(value, settings=settings)


def test_is_trusted_does_not_raise(workspace) -> None:
    base, settings = workspace
    assert is_trusted(str(base / "projects" / "notes.txt"), settings) is True
    assert is_trusted(str(base / "outside" / "secret.txt"), settings) is False


def test_scope_is_the_containing_folder() -> None:
    assert scope_for("D:/Projects/app/main.py").replace("\\", "/") == "D:/Projects/app"
    assert scope_for("D:/Projects/app").replace("\\", "/") == "D:/Projects/app"
