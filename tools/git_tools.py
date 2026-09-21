"""Local git tools (Spec §36).

These operate on a working copy with the ``git`` binary. Anything that talks to GitHub's API
(issues, pull requests, code search) lives in ``integrations/github`` — the specification asks
for the two to stay cleanly separated, because they need different credentials and carry
different risk.

``git push`` is its own tool with its own capability, so it can be set to ASK while commits
run automatically.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from core.enums import Capability, RiskLevel
from core.errors import ToolError
from core.logging_setup import get_logger
from tools.base import Tool, ToolContext, ToolResult
from tools.sandbox import resolve_path

log = get_logger("tools.git")

GIT_TIMEOUT = 120.0
MAX_DIFF_CHARS = 30_000


def _git_available() -> bool:
    return shutil.which("git") is not None


async def _git(repository: Path, *args: str, timeout: float = GIT_TIMEOUT) -> tuple[int, str, str]:
    """Run git in a repository. Never uses a shell, so arguments cannot be re-interpreted."""
    binary = shutil.which("git")
    if binary is None:
        raise ToolError(
            "git not found",
            user_message="git ist auf diesem System nicht installiert.",
        )
    process = await asyncio.create_subprocess_exec(
        binary, "-C", str(repository), *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Never let git open an interactive credential prompt: it would hang the tool.
        env={"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "PATH": _safe_path()},
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        raise ToolError(
            "git timed out",
            user_message=f"Der git-Befehl hat das Zeitlimit von {timeout:.0f}s überschritten.",
        ) from None
    return process.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")


def _safe_path() -> str:
    import os

    return os.environ.get("PATH", "")


class _GitTool(Tool):
    """Shared repository resolution."""

    def __init__(self) -> None:
        self.available = _git_available()
        self.unavailable_reason = "" if self.available else "git ist nicht installiert."

    def _repository(self, arguments: dict[str, Any], settings) -> Path:
        resolved = resolve_path(arguments["repository"], must_exist=True, settings=settings)
        path = resolved.path
        if not path.is_dir():
            raise ToolError(
                f"{path} is not a directory",
                user_message=f"'{path}' ist kein Ordner.",
            )
        if not (path / ".git").exists():
            raise ToolError(
                f"{path} is not a git repository",
                user_message=f"'{path}' ist kein Git-Repository.",
            )
        return path

    def scope(self, arguments: dict[str, Any]) -> str:
        return str(arguments.get("repository", self.name))


class GitStatusTool(_GitTool):
    name = "git_status"
    description = "Zeigt Branch, geänderte Dateien und den Zustand eines Git-Repositorys."
    risk_level = RiskLevel.SAFE_READ
    required_permission = Capability.FILE_READ
    input_schema = {
        "type": "object",
        "properties": {"repository": {"type": "string"}},
        "required": ["repository"],
    }

    def summarise(self, arguments: dict[str, Any]) -> str:
        return f"Git-Status von '{arguments.get('repository')}'"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        settings = context.settings
        repository = self._repository(arguments, settings)
        _, branch, _ = await _git(repository, "rev-parse", "--abbrev-ref", "HEAD")
        code, porcelain, stderr = await _git(repository, "status", "--porcelain=v1", "--branch")
        if code != 0:
            return ToolResult.failure(f"git status: {stderr.strip()}")

        lines = [line for line in porcelain.splitlines() if line.strip()]
        header = lines[0] if lines and lines[0].startswith("##") else ""
        changes = [line for line in lines if not line.startswith("##")]
        staged = [c for c in changes if c[:1] not in (" ", "?")]
        unstaged = [c for c in changes if c[1:2] != " " and not c.startswith("??")]
        untracked = [c for c in changes if c.startswith("??")]

        summary = (
            f"Branch: {branch.strip()}\n{header}\n"
            f"{len(staged)} vorgemerkt, {len(unstaged)} geändert, {len(untracked)} unversioniert\n"
            + ("\n".join(changes[:60]) if changes else "Arbeitsverzeichnis ist sauber.")
        )
        return ToolResult(
            content=summary,
            display={
                "summary": f"{branch.strip()}: {len(changes)} Änderung(en)",
                "branch": branch.strip(), "staged": len(staged),
                "unstaged": len(unstaged), "untracked": len(untracked),
                "files": [c[3:] for c in changes[:40]],
            },
        )


class GitDiffTool(_GitTool):
    name = "git_diff"
    description = "Zeigt die Änderungen im Repository (optional nur eine Datei)."
    risk_level = RiskLevel.SAFE_READ
    required_permission = Capability.FILE_READ
    input_schema = {
        "type": "object",
        "properties": {
            "repository": {"type": "string"},
            "path": {"type": "string", "description": "Optional: nur diese Datei"},
            "staged": {"type": "boolean", "default": False},
        },
        "required": ["repository"],
    }

    def summarise(self, arguments: dict[str, Any]) -> str:
        return f"Git-Diff von '{arguments.get('repository')}'"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        settings = context.settings
        repository = self._repository(arguments, settings)
        args = ["diff", "--no-color"]
        if arguments.get("staged"):
            args.append("--staged")
        if path := arguments.get("path"):
            args.extend(["--", path])

        code, diff, stderr = await _git(repository, *args)
        if code != 0:
            return ToolResult.failure(f"git diff: {stderr.strip()}")
        if not diff.strip():
            return ToolResult(content="Keine Änderungen.",
                              display={"summary": "Keine Änderungen", "lines": 0})

        truncated = len(diff) > MAX_DIFF_CHARS
        body = diff[:MAX_DIFF_CHARS] + ("\n[… Diff gekürzt …]" if truncated else "")
        added = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
        return ToolResult(
            content=body,
            display={"summary": f"+{added} / -{removed} Zeilen", "added": added,
                     "removed": removed, "truncated": truncated},
        )


class GitLogTool(_GitTool):
    name = "git_log"
    description = "Zeigt die letzten Commits eines Repositorys."
    risk_level = RiskLevel.SAFE_READ
    required_permission = Capability.FILE_READ
    input_schema = {
        "type": "object",
        "properties": {
            "repository": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 15},
        },
        "required": ["repository"],
    }

    def summarise(self, arguments: dict[str, Any]) -> str:
        return f"Git-Log von '{arguments.get('repository')}'"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        settings = context.settings
        repository = self._repository(arguments, settings)
        code, out, stderr = await _git(
            repository, "log", f"-{arguments.get('limit', 15)}",
            "--pretty=format:%h\t%an\t%ar\t%s", "--no-color",
        )
        if code != 0:
            return ToolResult.failure(f"git log: {stderr.strip()}")
        commits = [
            dict(zip(("hash", "author", "when", "subject"), line.split("\t", 3), strict=False))
            for line in out.splitlines() if line.strip()
        ]
        return ToolResult(
            content=out or "Keine Commits.",
            display={"summary": f"{len(commits)} Commit(s)", "commits": commits},
        )


class GitCommitTool(_GitTool):
    name = "git_commit"
    description = (
        "Erstellt einen Commit. Mit 'add_all' werden vorher alle Änderungen vorgemerkt. "
        "Pusht NICHT — dafür gibt es git_push."
    )
    risk_level = RiskLevel.WRITE
    required_permission = Capability.GIT_COMMIT
    input_schema = {
        "type": "object",
        "properties": {
            "repository": {"type": "string"},
            "message": {"type": "string", "minLength": 3},
            "add_all": {"type": "boolean", "default": False},
        },
        "required": ["repository", "message"],
    }

    def summarise(self, arguments: dict[str, Any]) -> str:
        prefix = "Alles vormerken und committen" if arguments.get("add_all") else "Committen"
        return f"{prefix} in '{arguments.get('repository')}': {arguments.get('message')}"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        settings = context.settings
        repository = self._repository(arguments, settings)
        if arguments.get("add_all"):
            code, _, stderr = await _git(repository, "add", "-A")
            if code != 0:
                return ToolResult.failure(f"git add: {stderr.strip()}")

        code, staged, _ = await _git(repository, "diff", "--staged", "--name-only")
        if not staged.strip():
            return ToolResult.failure(
                "Es sind keine Änderungen vorgemerkt. Nutze 'add_all' oder merke Dateien vor."
            )

        code, out, stderr = await _git(repository, "commit", "-m", arguments["message"])
        if code != 0:
            return ToolResult.failure(f"git commit: {(stderr or out).strip()}")

        _, commit_hash, _ = await _git(repository, "rev-parse", "--short", "HEAD")
        files = [f for f in staged.splitlines() if f.strip()]
        return ToolResult(
            content=f"Commit {commit_hash.strip()} erstellt mit {len(files)} Datei(en).\n{out}",
            display={"summary": f"Commit {commit_hash.strip()} ({len(files)} Dateien)",
                     "hash": commit_hash.strip(), "files": files[:40]},
        )


class GitPushTool(_GitTool):
    """Separate from commit: pushing is an outward-facing action (Spec §16, §36)."""

    name = "git_push"
    description = "Überträgt Commits zum Remote-Repository."
    risk_level = RiskLevel.EXTERNAL_WRITE
    required_permission = Capability.GIT_PUSH
    input_schema = {
        "type": "object",
        "properties": {
            "repository": {"type": "string"},
            "remote": {"type": "string", "default": "origin"},
            "branch": {"type": "string", "description": "Standard: der aktuelle Branch"},
        },
        "required": ["repository"],
    }

    def summarise(self, arguments: dict[str, Any]) -> str:
        return (f"PUSH nach '{arguments.get('remote', 'origin')}' "
                f"aus '{arguments.get('repository')}'")

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        settings = context.settings
        repository = self._repository(arguments, settings)
        branch = arguments.get("branch")
        if not branch:
            _, current, _ = await _git(repository, "rev-parse", "--abbrev-ref", "HEAD")
            branch = current.strip()
        remote = arguments.get("remote", "origin")

        code, out, stderr = await _git(repository, "push", "-u", remote, branch, timeout=180.0)
        output = (out + stderr).strip()
        if code != 0:
            return ToolResult.failure(f"git push ist fehlgeschlagen: {output[:600]}")
        return ToolResult(
            content=f"Nach {remote}/{branch} gepusht.\n{output}",
            display={"summary": f"Push nach {remote}/{branch}", "remote": remote, "branch": branch},
        )


class GitBranchTool(_GitTool):
    name = "git_branch"
    description = "Listet Branches oder erstellt und wechselt auf einen neuen Branch."
    risk_level = RiskLevel.WRITE
    required_permission = Capability.GIT_COMMIT
    input_schema = {
        "type": "object",
        "properties": {
            "repository": {"type": "string"},
            "create": {"type": "string", "description": "Name eines neuen Branches"},
            "switch_to": {"type": "string", "description": "Auf diesen Branch wechseln"},
        },
        "required": ["repository"],
    }

    def summarise(self, arguments: dict[str, Any]) -> str:
        if name := arguments.get("create"):
            return f"Branch '{name}' anlegen in '{arguments.get('repository')}'"
        if name := arguments.get("switch_to"):
            return f"Auf Branch '{name}' wechseln in '{arguments.get('repository')}'"
        return f"Branches auflisten in '{arguments.get('repository')}'"

    def risk_for(self, arguments: dict[str, Any]) -> tuple[RiskLevel, Capability]:
        # Listing branches changes nothing, so it should not need a write confirmation.
        if not arguments.get("create") and not arguments.get("switch_to"):
            return RiskLevel.SAFE_READ, Capability.FILE_READ
        return self.risk_level, self.required_permission

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        settings = context.settings
        repository = self._repository(arguments, settings)

        if name := arguments.get("create"):
            code, out, stderr = await _git(repository, "checkout", "-b", name)
            if code != 0:
                return ToolResult.failure(f"git checkout -b: {(stderr or out).strip()}")
            return ToolResult(content=f"Branch '{name}' angelegt und ausgecheckt.",
                              display={"summary": f"Branch {name} angelegt", "branch": name})

        if name := arguments.get("switch_to"):
            code, out, stderr = await _git(repository, "checkout", name)
            if code != 0:
                return ToolResult.failure(f"git checkout: {(stderr or out).strip()}")
            return ToolResult(content=f"Auf Branch '{name}' gewechselt.",
                              display={"summary": f"Branch {name}", "branch": name})

        code, out, stderr = await _git(repository, "branch", "--all", "--no-color")
        if code != 0:
            return ToolResult.failure(f"git branch: {stderr.strip()}")
        branches = [line.strip() for line in out.splitlines() if line.strip()]
        current = next((b[2:] for b in branches if b.startswith("* ")), "")
        return ToolResult(
            content=out,
            display={"summary": f"{len(branches)} Branch(es), aktuell: {current}",
                     "current": current, "branches": [b.lstrip("* ") for b in branches]},
        )


GIT_TOOLS = [
    GitStatusTool(), GitDiffTool(), GitLogTool(), GitCommitTool(), GitPushTool(), GitBranchTool(),
]
