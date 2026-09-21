"""Terminal tools (Spec §47).

Commands are **classified**, not merely matched against a blacklist. The classifier parses the
command line, walks every stage of a pipeline and every element of a chain (``;``, ``&&``,
``|``), and classifies each one. The whole command inherits the highest class found, so
``ls && rm -rf /`` is DESTRUCTIVE even though it starts with a harmless ``ls``.

The class then decides the risk level, and the permission engine decides what happens — the
tool itself never waves a command through.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import sys
import time
from dataclasses import dataclass
from typing import Any

from core.enums import Capability, CommandClass, RiskLevel
from core.errors import ToolError
from core.logging_setup import get_logger
from tools.base import Tool, ToolContext, ToolResult
from tools.sandbox import resolve_path, trusted_roots

log = get_logger("tools.terminal")

MAX_OUTPUT_CHARS = 60_000
DEFAULT_TIMEOUT = 120.0

# Programs grouped by what they do to the system. Anything unknown is treated as WRITE —
# the safe default, because an unrecognised program may do anything.
READ_ONLY_COMMANDS = frozenset({
    "ls", "dir", "cat", "type", "head", "tail", "less", "more", "pwd", "cd", "echo", "which",
    "where", "whoami", "hostname", "date", "uptime", "df", "du", "stat", "file", "wc",
    "find", "grep", "rg", "ripgrep", "fgrep", "egrep", "tree", "diff", "cmp", "sort", "uniq",
    "ps", "top", "env", "printenv", "python", "python3", "node", "java", "dotnet",
    "git", "gh", "docker", "kubectl", "pip", "npm", "yarn", "pnpm", "cargo", "go",
    "systeminfo", "ver", "tasklist", "ipconfig", "ifconfig", "ping", "nslookup", "curl",
    "wget", "jq", "yq", "md5sum", "sha256sum", "openssl", "ruff", "pytest", "mypy", "black",
})

# Subcommands that turn an otherwise read-only program into a writing one.
WRITING_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "git": frozenset({"commit", "push", "reset", "checkout", "merge", "rebase", "clean",
                      "stash", "apply", "am", "cherry-pick", "revert", "tag", "branch",
                      "config", "remote", "init", "clone", "pull", "fetch", "add", "rm", "mv"}),
    "pip": frozenset({"install", "uninstall"}),
    "npm": frozenset({"install", "i", "uninstall", "publish", "update", "ci"}),
    "yarn": frozenset({"add", "remove", "install", "publish", "upgrade"}),
    "pnpm": frozenset({"add", "remove", "install", "publish", "update"}),
    "cargo": frozenset({"install", "publish", "build", "run", "clean"}),
    "go": frozenset({"install", "build", "get", "mod", "clean"}),
    "docker": frozenset({"run", "rm", "rmi", "build", "push", "stop", "kill", "prune",
                         "exec", "compose"}),
    "kubectl": frozenset({"apply", "delete", "create", "patch", "scale", "drain"}),
    "gh": frozenset({"pr", "issue", "release", "repo", "auth", "api"}),
}

WRITE_COMMANDS = frozenset({
    "cp", "copy", "mv", "move", "ren", "rename", "mkdir", "md", "touch", "tee", "ln",
    "tar", "zip", "unzip", "7z", "gzip", "gunzip", "xcopy", "robocopy", "sed", "awk",
})

DESTRUCTIVE_COMMANDS = frozenset({
    "rm", "del", "erase", "rd", "rmdir", "shred", "mkfs", "fdisk", "diskpart", "format",
    "dd", "truncate", "cipher",
})

SYSTEM_COMMANDS = frozenset({
    "kill", "killall", "taskkill", "pkill", "shutdown", "reboot", "restart", "halt",
    "systemctl", "service", "sc", "net", "netsh", "reg", "regedit", "bcdedit", "wmic",
    "schtasks", "crontab", "mount", "umount", "chmod", "chown", "chgrp", "setx",
})

ADMIN_COMMANDS = frozenset({"sudo", "su", "doas", "runas", "elevate", "gsudo", "pkexec"})

# Patterns that are destructive regardless of the program, e.g. a wildcard delete at a root.
_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\s+(-[a-z]*\s+)*-?[a-z]*[rf][a-z]*\s+/\s*$"), "rm -rf auf dem Wurzelverzeichnis"),
    (re.compile(r"\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r"), "rekursives Löschen mit Force"),
    (re.compile(r"\b(format|mkfs)\b"), "Datenträger formatieren"),
    (re.compile(r":\(\)\s*\{.*\|.*&.*\}\s*;?\s*:"), "Fork-Bombe"),
    (re.compile(r">\s*/dev/[sh]d[a-z]"), "direktes Schreiben auf ein Blockgerät"),
    (re.compile(r"\bdd\b.*\bof=/dev/"), "dd auf ein Blockgerät"),
    (re.compile(r"\bcurl\b[^|]*\|\s*(ba|z|k)?sh\b"), "Skript aus dem Netz direkt ausführen"),
    (re.compile(r"\bwget\b[^|]*\|\s*(ba|z|k)?sh\b"), "Skript aus dem Netz direkt ausführen"),
]

_SPLIT_OPERATORS = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")

CLASS_RISK: dict[CommandClass, RiskLevel] = {
    CommandClass.READ_ONLY: RiskLevel.SAFE_READ,
    CommandClass.WRITE: RiskLevel.WRITE,
    CommandClass.SYSTEM: RiskLevel.SYSTEM_CONTROL,
    CommandClass.DESTRUCTIVE: RiskLevel.DESTRUCTIVE,
    CommandClass.ADMIN: RiskLevel.PRIVILEGED,
}

CLASS_ORDER = {
    CommandClass.READ_ONLY: 0,
    CommandClass.WRITE: 1,
    CommandClass.SYSTEM: 2,
    CommandClass.DESTRUCTIVE: 3,
    CommandClass.ADMIN: 4,
}

CLASS_CAPABILITY: dict[CommandClass, Capability] = {
    CommandClass.READ_ONLY: Capability.TERMINAL_READ,
    CommandClass.WRITE: Capability.TERMINAL_WRITE,
    CommandClass.SYSTEM: Capability.TERMINAL_WRITE,
    CommandClass.DESTRUCTIVE: Capability.TERMINAL_WRITE,
    CommandClass.ADMIN: Capability.TERMINAL_ADMIN,
}


@dataclass(slots=True)
class Classification:
    command_class: CommandClass
    reasons: list[str]
    programs: list[str]

    @property
    def risk(self) -> RiskLevel:
        return CLASS_RISK[self.command_class]

    @property
    def capability(self) -> Capability:
        return CLASS_CAPABILITY[self.command_class]

    def to_dict(self) -> dict[str, Any]:
        return {
            "class": self.command_class.value,
            "risk": self.risk.value,
            "reasons": self.reasons,
            "programs": self.programs,
        }


def _classify_segment(segment: str) -> tuple[CommandClass, str, str]:
    """Classify one pipeline stage. Returns (class, reason, program)."""
    try:
        parts = shlex.split(segment, posix=sys.platform != "win32")
    except ValueError:
        # Unbalanced quotes: fall back to a rough split rather than accepting blindly.
        parts = segment.split()
    if not parts:
        return CommandClass.READ_ONLY, "", ""

    # Skip leading VAR=value assignments.
    index = 0
    while index < len(parts) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", parts[index]):
        index += 1
    if index >= len(parts):
        return CommandClass.WRITE, "Setzt Umgebungsvariablen", ""

    program = os.path.basename(parts[index]).lower()
    program = program[:-4] if program.endswith(".exe") else program
    arguments = parts[index + 1 :]

    if program in ADMIN_COMMANDS:
        return CommandClass.ADMIN, f"'{program}' führt mit erhöhten Rechten aus", program
    if program in DESTRUCTIVE_COMMANDS:
        return CommandClass.DESTRUCTIVE, f"'{program}' löscht oder überschreibt Daten", program
    if program in SYSTEM_COMMANDS:
        return CommandClass.SYSTEM, f"'{program}' verändert den Systemzustand", program
    if program in WRITE_COMMANDS:
        return CommandClass.WRITE, f"'{program}' verändert Dateien", program

    if program in READ_ONLY_COMMANDS:
        subcommands = WRITING_SUBCOMMANDS.get(program)
        if subcommands:
            first = next((a for a in arguments if not a.startswith("-")), "")
            if first.lower() in subcommands:
                return (CommandClass.WRITE,
                        f"'{program} {first}' verändert etwas", program)
        # Running arbitrary code through an interpreter is a write, not a read.
        if program in ("python", "python3", "node", "java", "dotnet") and any(
            not a.startswith("-") for a in arguments
        ):
            return CommandClass.WRITE, f"'{program}' führt ein Skript aus", program
        return CommandClass.READ_ONLY, "", program

    return CommandClass.WRITE, f"'{program}' ist unbekannt und gilt vorsichtshalber als schreibend", program


def classify_command(command: str) -> Classification:
    """Classify a full command line, taking the highest class of all its parts."""
    text = (command or "").strip()
    if not text:
        return Classification(CommandClass.READ_ONLY, [], [])

    reasons: list[str] = []
    programs: list[str] = []
    worst = CommandClass.READ_ONLY

    # Whole-line danger patterns first: these beat any per-program classification.
    for pattern, label in _DANGEROUS_PATTERNS:
        if pattern.search(text):
            reasons.append(f"Gefährliches Muster erkannt: {label}")
            worst = CommandClass.DESTRUCTIVE

    for segment in _SPLIT_OPERATORS.split(text):
        segment = segment.strip()
        if not segment:
            continue
        # Redirection into a file is a write even for an otherwise read-only program.
        if re.search(r"(?<![0-9])>{1,2}(?!\s*&)", segment):
            if CLASS_ORDER[CommandClass.WRITE] > CLASS_ORDER[worst]:
                worst = CommandClass.WRITE
            reasons.append("Ausgabe wird in eine Datei umgeleitet")

        command_class, reason, program = _classify_segment(segment)
        if program:
            programs.append(program)
        if reason:
            reasons.append(reason)
        if CLASS_ORDER[command_class] > CLASS_ORDER[worst]:
            worst = command_class

    return Classification(worst, reasons, programs)


async def _run_process(
    argv: list[str] | str,
    *,
    cwd: str,
    timeout: float,
    shell: bool,
    cancel: asyncio.Event | None,
) -> tuple[int | None, str, str, bool]:
    """Run a process, enforcing a timeout and honouring cancellation."""
    if shell:
        process = await asyncio.create_subprocess_shell(
            argv if isinstance(argv, str) else " ".join(argv),
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    else:
        process = await asyncio.create_subprocess_exec(
            *(argv if isinstance(argv, list) else shlex.split(argv)),
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    communicate = asyncio.create_task(process.communicate())
    waiters: list[asyncio.Task] = [communicate]
    cancel_task: asyncio.Task | None = None
    if cancel is not None:
        cancel_task = asyncio.create_task(cancel.wait())
        waiters.append(cancel_task)

    done, pending = await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)

    if communicate in done:
        for task in pending:
            task.cancel()
        stdout, stderr = communicate.result()
        return process.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace"), False

    # Timed out or cancelled: terminate, then kill if it does not go quietly.
    for task in pending:
        task.cancel()
    was_cancelled = cancel_task is not None and cancel_task in done
    try:
        process.terminate()
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except (ProcessLookupError, TimeoutError):
        try:
            process.kill()
        except ProcessLookupError:
            pass
    return None, "", "", was_cancelled


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    half = MAX_OUTPUT_CHARS // 2
    return (
        text[:half] + f"\n… [{len(text) - MAX_OUTPUT_CHARS} Zeichen ausgelassen] …\n" + text[-half:],
        True,
    )


class _BaseCommandTool(Tool):
    """Shared execution path for the command tools."""

    shell = True

    def _working_directory(self, arguments: dict[str, Any], settings) -> str:
        raw = arguments.get("working_directory")
        if raw:
            return str(resolve_path(raw, must_exist=True, settings=settings).path)
        roots = trusted_roots(settings)
        if not roots:
            raise ToolError(
                "No trusted folder configured",
                user_message="Es ist kein vertrauenswürdiger Ordner freigegeben, in dem ich "
                             "einen Befehl ausführen könnte.",
            )
        return str(roots[0])

    async def _execute(
        self, command: list[str] | str, arguments: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        cwd = self._working_directory(arguments, context.settings)
        timeout = float(arguments.get("timeout", DEFAULT_TIMEOUT))
        started = time.perf_counter()

        code, stdout, stderr, cancelled = await _run_process(
            command, cwd=cwd, timeout=timeout, shell=self.shell, cancel=context.cancel
        )
        duration_ms = int((time.perf_counter() - started) * 1000)

        if cancelled:
            return ToolResult.failure("Der Befehl wurde abgebrochen.", cancelled=True)
        if code is None:
            return ToolResult.failure(
                f"Der Befehl hat das Zeitlimit von {timeout:.0f}s überschritten und wurde beendet.",
                timeout=True,
            )

        out, out_truncated = _truncate(stdout)
        err, err_truncated = _truncate(stderr)
        body = f"exit_code={code}\n"
        if out.strip():
            body += f"--- stdout ---\n{out}\n"
        if err.strip():
            body += f"--- stderr ---\n{err}\n"
        if not out.strip() and not err.strip():
            body += "(keine Ausgabe)\n"

        return ToolResult(
            ok=code == 0,
            content=body,
            error="" if code == 0 else f"Exit-Code {code}",
            display={
                "summary": f"exit={code} in {duration_ms} ms",
                "exit_code": code, "duration_ms": duration_ms, "cwd": cwd,
                "stdout_preview": out[:1500], "stderr_preview": err[:1500],
                "truncated": out_truncated or err_truncated,
            },
        )


class RunCommandTool(_BaseCommandTool):
    name = "run_command"
    description = (
        "Führt einen Shell-Befehl in einem freigegebenen Ordner aus und gibt Exit-Code, "
        "stdout und stderr zurück."
    )
    # The declared ceiling. risk_for() refines it per command, upwards as well as downwards:
    # "ls" is a SAFE_READ, "rm -rf" is DESTRUCTIVE, "sudo" is PRIVILEGED.
    risk_level = RiskLevel.PRIVILEGED
    required_permission = Capability.TERMINAL_WRITE
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "minLength": 1},
            "working_directory": {"type": "string"},
            "timeout": {"type": "number", "minimum": 1, "maximum": 900, "default": DEFAULT_TIMEOUT},
        },
        "required": ["command"],
    }

    def classify(self, arguments: dict[str, Any]) -> Classification:
        return classify_command(arguments.get("command", ""))

    def summarise(self, arguments: dict[str, Any]) -> str:
        classification = self.classify(arguments)
        summary = f"Befehl ausführen [{classification.command_class.value}]: {arguments.get('command')}"
        if classification.reasons:
            summary += f"\nEinstufung: {'; '.join(classification.reasons[:3])}"
        return summary

    def scope(self, arguments: dict[str, Any]) -> str:
        programs = self.classify(arguments).programs
        return f"run_command:{programs[0]}" if programs else "run_command"

    def risk_for(self, arguments: dict[str, Any]) -> tuple[RiskLevel, Capability]:
        classification = self.classify(arguments)
        return classification.risk, classification.capability

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        return await self._execute(arguments["command"], arguments, context)


class RunPowerShellTool(_BaseCommandTool):
    name = "run_powershell"
    description = "Führt ein PowerShell-Skript aus (nur Windows)."
    risk_level = RiskLevel.SYSTEM_CONTROL
    required_permission = Capability.TERMINAL_WRITE
    input_schema = {
        "type": "object",
        "properties": {
            "script": {"type": "string", "minLength": 1},
            "working_directory": {"type": "string"},
            "timeout": {"type": "number", "minimum": 1, "maximum": 900, "default": DEFAULT_TIMEOUT},
        },
        "required": ["script"],
    }
    shell = False

    def __init__(self) -> None:
        self.available = sys.platform == "win32"
        self.unavailable_reason = (
            "" if self.available else "PowerShell gibt es nur unter Windows."
        )

    def summarise(self, arguments: dict[str, Any]) -> str:
        return f"PowerShell ausführen: {str(arguments.get('script'))[:120]}"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        return await self._execute(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", arguments["script"]],
            arguments, context,
        )


class RunPythonTool(_BaseCommandTool):
    name = "run_python"
    description = "Führt ein Python-Skript aus und gibt die Ausgabe zurück."
    risk_level = RiskLevel.WRITE
    required_permission = Capability.TERMINAL_WRITE
    input_schema = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "minLength": 1},
            "working_directory": {"type": "string"},
            "timeout": {"type": "number", "minimum": 1, "maximum": 900, "default": DEFAULT_TIMEOUT},
        },
        "required": ["code"],
    }
    shell = False

    def summarise(self, arguments: dict[str, Any]) -> str:
        first_line = str(arguments.get("code", "")).strip().splitlines()[:1]
        return f"Python ausführen: {first_line[0][:100] if first_line else ''} …"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        return await self._execute(
            [sys.executable, "-c", arguments["code"]], arguments, context
        )


TERMINAL_TOOLS = [RunCommandTool(), RunPowerShellTool(), RunPythonTool()]
