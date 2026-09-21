"""File tools (Spec §45, §46).

Every path goes through :mod:`tools.sandbox`, so nothing here can reach outside the trusted
folders. Deleting is a separate tool with a higher risk level, exactly as the specification
requires, and never recurses unless the caller asks for it explicitly.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from typing import Any

from core.enums import Capability, RiskLevel
from core.errors import ToolError
from core.logging_setup import get_logger
from tools.base import Tool, ToolContext, ToolResult
from tools.sandbox import MAX_READ_BYTES, resolve_path, scope_for

log = get_logger("tools.files")

TEXT_SUFFIXES = {
    ".txt", ".md", ".json", ".xml", ".csv", ".tsv", ".html", ".htm", ".css", ".js", ".ts",
    ".py", ".log", ".yaml", ".yml", ".ini", ".cfg", ".toml", ".sql", ".sh", ".ps1", ".bat",
    ".java", ".c", ".h", ".cpp", ".hpp", ".rs", ".go", ".rb", ".php", ".env.example", ".rpt",
    ".gitignore", ".editorconfig", ".conf",
}


def _looks_textual(path: Path) -> bool:
    if path.suffix.lower() in TEXT_SUFFIXES or path.name.lower() in TEXT_SUFFIXES:
        return True
    try:
        with path.open("rb") as handle:
            sample = handle.read(4096)
    except OSError:
        return False
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        try:
            sample.decode("cp1252")
        except UnicodeDecodeError:
            return False
    return True


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    if len(data) > MAX_READ_BYTES:
        data = data[:MAX_READ_BYTES]
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GB"


class ListDirectoryTool(Tool):
    name = "list_directory"
    description = "Listet den Inhalt eines Ordners auf (Dateien und Unterordner)."
    risk_level = RiskLevel.SAFE_READ
    required_permission = Capability.FILE_READ
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Pfad des Ordners"},
            "pattern": {"type": "string", "description": "Optionaler Glob-Filter, z. B. *.xml"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
        },
        "required": ["path"],
    }

    def summarise(self, arguments: dict[str, Any]) -> str:
        return f"Ordner '{arguments.get('path')}' auflisten"

    def scope(self, arguments: dict[str, Any]) -> str:
        return scope_for(arguments.get("path", ""))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        settings = context.settings
        resolved = resolve_path(arguments["path"], must_exist=True, settings=settings)
        if not resolved.path.is_dir():
            return ToolResult.failure(f"'{resolved.path}' ist kein Ordner.")

        pattern = arguments.get("pattern") or "*"
        limit = arguments.get("limit", 200)
        entries: list[dict[str, Any]] = []
        for child in sorted(resolved.path.glob(pattern), key=lambda p: (p.is_file(), p.name.lower())):
            try:
                stat = child.stat()
            except OSError:
                continue
            entries.append({
                "name": child.name,
                "kind": "dir" if child.is_dir() else "file",
                "size": stat.st_size if child.is_file() else None,
                "modified": stat.st_mtime,
            })
            if len(entries) >= limit:
                break

        lines = [
            f"{'[DIR] ' if e['kind'] == 'dir' else '      '}{e['name']}"
            + (f"  ({_human_size(e['size'])})" if e["size"] is not None else "")
            for e in entries
        ]
        return ToolResult(
            content=f"{resolved.path} — {len(entries)} Einträge:\n" + "\n".join(lines),
            display={"summary": f"{len(entries)} Einträge in {resolved.path.name}",
                     "path": str(resolved.path), "entries": entries[:100]},
        )


class ReadFileTool(Tool):
    name = "read_file"
    description = "Liest den Inhalt einer Textdatei. Optional nur einen Zeilenbereich."
    risk_level = RiskLevel.SAFE_READ
    required_permission = Capability.FILE_READ
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
        },
        "required": ["path"],
    }

    def summarise(self, arguments: dict[str, Any]) -> str:
        return f"Datei '{arguments.get('path')}' lesen"

    def scope(self, arguments: dict[str, Any]) -> str:
        return scope_for(arguments.get("path", ""))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        settings = context.settings
        resolved = resolve_path(arguments["path"], must_exist=True, settings=settings)
        path = resolved.path
        if path.is_dir():
            return ToolResult.failure(f"'{path}' ist ein Ordner, keine Datei.")
        size = path.stat().st_size
        if not _looks_textual(path):
            return ToolResult.failure(
                f"'{path.name}' ist keine Textdatei ({_human_size(size)}). "
                "Binärdateien kann ich nicht als Text lesen."
            )

        text = _read_text(path)
        truncated = size > MAX_READ_BYTES
        lines = text.splitlines()
        start = arguments.get("start_line")
        end = arguments.get("end_line")
        if start or end:
            lines = lines[(start or 1) - 1 : (end or len(lines))]
            text = "\n".join(lines)

        return ToolResult(
            content=text + ("\n[… Datei ist größer als das Leselimit und wurde gekürzt …]" if truncated else ""),
            display={
                "summary": f"{path.name} gelesen ({len(lines)} Zeilen, {_human_size(size)})",
                "path": str(path), "lines": len(lines), "size": size, "truncated": truncated,
            },
        )


class SearchFilesTool(Tool):
    name = "search_files"
    description = "Sucht Dateien nach Name und optional nach Textinhalt."
    risk_level = RiskLevel.SAFE_READ
    required_permission = Capability.FILE_READ
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Ordner, in dem gesucht wird"},
            "name_pattern": {"type": "string", "default": "*"},
            "contains": {"type": "string", "description": "Optionaler Text, der vorkommen muss"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
        },
        "required": ["path"],
    }

    def summarise(self, arguments: dict[str, Any]) -> str:
        needle = arguments.get("contains")
        base = f"In '{arguments.get('path')}' nach '{arguments.get('name_pattern', '*')}' suchen"
        return base + (f", das '{needle}' enthält" if needle else "")

    def scope(self, arguments: dict[str, Any]) -> str:
        return scope_for(arguments.get("path", ""))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        settings = context.settings
        resolved = resolve_path(arguments["path"], must_exist=True, settings=settings)
        needle = (arguments.get("contains") or "").lower()
        limit = arguments.get("limit", 50)
        hits: list[dict[str, Any]] = []

        for candidate in resolved.path.rglob(arguments.get("name_pattern") or "*"):
            if context.cancel is not None and context.cancel.is_set():
                break
            if not candidate.is_file():
                continue
            if needle:
                if not _looks_textual(candidate):
                    continue
                try:
                    if needle not in _read_text(candidate).lower():
                        continue
                except OSError:
                    continue
            hits.append({"path": str(candidate), "name": candidate.name,
                         "size": candidate.stat().st_size})
            if len(hits) >= limit:
                break

        if not hits:
            return ToolResult(
                content="Keine Treffer.",
                display={"summary": "Keine Treffer", "hits": []},
            )
        return ToolResult(
            content=f"{len(hits)} Treffer:\n" + "\n".join(h["path"] for h in hits),
            display={"summary": f"{len(hits)} Treffer", "hits": hits[:50]},
        )


class WriteFileTool(Tool):
    name = "write_file"
    description = "Schreibt Text in eine Datei. Erstellt sie, falls sie nicht existiert."
    risk_level = RiskLevel.WRITE
    required_permission = Capability.FILE_WRITE
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "mode": {"type": "string", "enum": ["overwrite", "append"], "default": "overwrite"},
        },
        "required": ["path", "content"],
    }

    def summarise(self, arguments: dict[str, Any]) -> str:
        mode = "anhängen an" if arguments.get("mode") == "append" else "überschreiben:"
        size = len(arguments.get("content", ""))
        return f"Datei {mode} '{arguments.get('path')}' ({size} Zeichen)"

    def scope(self, arguments: dict[str, Any]) -> str:
        return scope_for(arguments.get("path", ""))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        settings = context.settings
        resolved = resolve_path(arguments["path"], for_write=True, settings=settings)
        path = resolved.path
        path.parent.mkdir(parents=True, exist_ok=True)
        content = arguments["content"]
        existed = path.exists()
        previous_size = path.stat().st_size if existed else 0

        if arguments.get("mode") == "append":
            with path.open("a", encoding="utf-8") as handle:
                handle.write(content)
        else:
            path.write_text(content, encoding="utf-8")

        return ToolResult(
            content=f"{path} geschrieben ({len(content)} Zeichen, "
                    f"{'angehängt' if arguments.get('mode') == 'append' else 'überschrieben'}).",
            display={
                "summary": f"{path.name} geschrieben",
                "path": str(path), "bytes": path.stat().st_size,
                "created": not existed, "previous_size": previous_size,
            },
        )


class PatchFileTool(Tool):
    name = "patch_file"
    description = (
        "Ersetzt einen exakten Textabschnitt in einer Datei. Sicherer als die ganze Datei "
        "zu überschreiben, weil der alte Text eindeutig vorkommen muss."
    )
    risk_level = RiskLevel.WRITE
    required_permission = Capability.FILE_WRITE
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string", "minLength": 1},
            "new_text": {"type": "string"},
        },
        "required": ["path", "old_text", "new_text"],
    }

    def summarise(self, arguments: dict[str, Any]) -> str:
        return f"Abschnitt in '{arguments.get('path')}' ersetzen"

    def scope(self, arguments: dict[str, Any]) -> str:
        return scope_for(arguments.get("path", ""))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        settings = context.settings
        resolved = resolve_path(arguments["path"], must_exist=True, for_write=True, settings=settings)
        text = _read_text(resolved.path)
        old = arguments["old_text"]
        occurrences = text.count(old)

        if occurrences == 0:
            return ToolResult.failure(
                "Der zu ersetzende Text kommt in der Datei nicht vor. "
                "Lies die Datei zuerst und verwende den exakten Wortlaut."
            )
        if occurrences > 1:
            return ToolResult.failure(
                f"Der zu ersetzende Text kommt {occurrences}-mal vor und ist damit nicht "
                "eindeutig. Nimm mehr Kontext dazu."
            )

        resolved.path.write_text(text.replace(old, arguments["new_text"], 1), encoding="utf-8")
        return ToolResult(
            content=f"{resolved.path}: ein Abschnitt ersetzt.",
            display={"summary": f"{resolved.path.name} angepasst", "path": str(resolved.path),
                     "removed_lines": old.count('\n') + 1,
                     "added_lines": arguments['new_text'].count('\n') + 1},
        )


class CreateDirectoryTool(Tool):
    name = "create_directory"
    description = "Erstellt einen Ordner (inklusive übergeordneter Ordner)."
    risk_level = RiskLevel.WRITE
    required_permission = Capability.FILE_WRITE
    input_schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    def summarise(self, arguments: dict[str, Any]) -> str:
        return f"Ordner '{arguments.get('path')}' anlegen"

    def scope(self, arguments: dict[str, Any]) -> str:
        return scope_for(arguments.get("path", ""))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        settings = context.settings
        resolved = resolve_path(arguments["path"], for_write=True, settings=settings)
        existed = resolved.path.exists()
        resolved.path.mkdir(parents=True, exist_ok=True)
        return ToolResult(
            content=f"{resolved.path} {'existierte bereits' if existed else 'wurde angelegt'}.",
            display={"summary": f"Ordner {resolved.path.name}", "path": str(resolved.path),
                     "created": not existed},
        )


class _TwoPathTool(Tool):
    """Shared plumbing for copy and move."""

    input_schema = {
        "type": "object",
        "properties": {"source": {"type": "string"}, "destination": {"type": "string"}},
        "required": ["source", "destination"],
    }

    def scope(self, arguments: dict[str, Any]) -> str:
        return scope_for(arguments.get("destination", ""))

    def _resolve_both(self, arguments: dict[str, Any], settings):
        source = resolve_path(arguments["source"], must_exist=True, settings=settings)
        destination = resolve_path(arguments["destination"], for_write=True, settings=settings)
        if destination.path.is_dir():
            destination = resolve_path(
                str(destination.path / source.path.name), for_write=True
            )
        return source, destination


class CopyFileTool(_TwoPathTool):
    name = "copy_file"
    description = "Kopiert eine Datei oder einen Ordner."
    risk_level = RiskLevel.WRITE
    required_permission = Capability.FILE_WRITE

    def summarise(self, arguments: dict[str, Any]) -> str:
        return f"'{arguments.get('source')}' nach '{arguments.get('destination')}' kopieren"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        settings = context.settings
        source, destination = self._resolve_both(arguments, settings)
        if source.path.is_dir():
            shutil.copytree(source.path, destination.path, dirs_exist_ok=True)
        else:
            destination.path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source.path, destination.path)
        return ToolResult(
            content=f"Kopiert: {source.path} -> {destination.path}",
            display={"summary": f"{source.path.name} kopiert",
                     "source": str(source.path), "destination": str(destination.path)},
        )


class MoveFileTool(_TwoPathTool):
    name = "move_file"
    description = "Verschiebt oder benennt eine Datei bzw. einen Ordner um."
    risk_level = RiskLevel.WRITE
    required_permission = Capability.FILE_WRITE

    def summarise(self, arguments: dict[str, Any]) -> str:
        return f"'{arguments.get('source')}' nach '{arguments.get('destination')}' verschieben"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        settings = context.settings
        source, destination = self._resolve_both(arguments, settings)
        destination.path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source.path), str(destination.path))
        return ToolResult(
            content=f"Verschoben: {source.path} -> {destination.path}",
            display={"summary": f"{source.path.name} verschoben",
                     "source": str(source.path), "destination": str(destination.path)},
        )


class DeleteFileTool(Tool):
    """Deliberately separate from the other file tools, with a higher risk level (Spec §45)."""

    name = "delete_file"
    description = "Löscht eine Datei. Ordner brauchen delete_directory."
    risk_level = RiskLevel.DESTRUCTIVE
    required_permission = Capability.FILE_DELETE
    input_schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    def summarise(self, arguments: dict[str, Any]) -> str:
        return f"Datei LÖSCHEN: '{arguments.get('path')}'"

    def scope(self, arguments: dict[str, Any]) -> str:
        return scope_for(arguments.get("path", ""))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        settings = context.settings
        resolved = resolve_path(arguments["path"], must_exist=True, for_write=True, settings=settings)
        if resolved.path.is_dir():
            return ToolResult.failure(
                f"'{resolved.path}' ist ein Ordner. Nutze delete_directory."
            )
        size = resolved.path.stat().st_size
        resolved.path.unlink()
        return ToolResult(
            content=f"{resolved.path} wurde gelöscht.",
            display={"summary": f"{resolved.path.name} gelöscht", "path": str(resolved.path),
                     "bytes": size},
        )


class DeleteDirectoryTool(Tool):
    name = "delete_directory"
    description = "Löscht einen Ordner. Ohne 'recursive' nur, wenn er leer ist."
    risk_level = RiskLevel.DESTRUCTIVE
    required_permission = Capability.FILE_DELETE
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "recursive": {"type": "boolean", "default": False},
        },
        "required": ["path"],
    }

    def summarise(self, arguments: dict[str, Any]) -> str:
        if arguments.get("recursive"):
            return f"Ordner MIT GESAMTEM INHALT LÖSCHEN: '{arguments.get('path')}'"
        return f"Leeren Ordner löschen: '{arguments.get('path')}'"

    def scope(self, arguments: dict[str, Any]) -> str:
        return scope_for(arguments.get("path", ""))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        settings = context.settings
        resolved = resolve_path(arguments["path"], must_exist=True, for_write=True, settings=settings)
        if not resolved.path.is_dir():
            return ToolResult.failure(f"'{resolved.path}' ist kein Ordner.")
        # Deleting a trusted root itself is almost certainly a mistake, so it is refused.
        if resolved.path == resolved.root:
            return ToolResult.failure(
                f"'{resolved.path}' ist ein freigegebener Stammordner und wird nicht gelöscht."
            )

        if arguments.get("recursive"):
            count = sum(1 for _ in resolved.path.rglob("*"))
            shutil.rmtree(resolved.path)
            return ToolResult(
                content=f"{resolved.path} und {count} Einträge wurden gelöscht.",
                display={"summary": f"{resolved.path.name} gelöscht", "entries": count},
            )
        try:
            resolved.path.rmdir()
        except OSError as exc:
            raise ToolError(
                str(exc),
                user_message=f"'{resolved.path}' ist nicht leer. Für rekursives Löschen "
                             "musst du 'recursive' setzen.",
            ) from exc
        return ToolResult(
            content=f"{resolved.path} wurde gelöscht.",
            display={"summary": f"{resolved.path.name} gelöscht"},
        )


class ArchiveTool(Tool):
    name = "create_archive"
    description = "Packt eine Datei oder einen Ordner in ein ZIP-Archiv."
    risk_level = RiskLevel.WRITE
    required_permission = Capability.FILE_WRITE
    input_schema = {
        "type": "object",
        "properties": {"source": {"type": "string"}, "destination": {"type": "string"}},
        "required": ["source", "destination"],
    }

    def summarise(self, arguments: dict[str, Any]) -> str:
        return f"'{arguments.get('source')}' nach '{arguments.get('destination')}' packen"

    def scope(self, arguments: dict[str, Any]) -> str:
        return scope_for(arguments.get("destination", ""))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        settings = context.settings
        source = resolve_path(arguments["source"], must_exist=True, settings=settings)
        destination = resolve_path(arguments["destination"], for_write=True, settings=settings)
        count = 0
        with zipfile.ZipFile(destination.path, "w", zipfile.ZIP_DEFLATED) as archive:
            if source.path.is_dir():
                for item in source.path.rglob("*"):
                    if item.is_file():
                        archive.write(item, item.relative_to(source.path))
                        count += 1
            else:
                archive.write(source.path, source.path.name)
                count = 1
        return ToolResult(
            content=f"{count} Datei(en) nach {destination.path} gepackt.",
            display={"summary": f"{count} Datei(en) gepackt", "path": str(destination.path)},
        )


class ExtractArchiveTool(Tool):
    name = "extract_archive"
    description = "Entpackt ein ZIP-Archiv in einen Ordner."
    risk_level = RiskLevel.WRITE
    required_permission = Capability.FILE_WRITE
    input_schema = {
        "type": "object",
        "properties": {"source": {"type": "string"}, "destination": {"type": "string"}},
        "required": ["source", "destination"],
    }

    def summarise(self, arguments: dict[str, Any]) -> str:
        return f"'{arguments.get('source')}' nach '{arguments.get('destination')}' entpacken"

    def scope(self, arguments: dict[str, Any]) -> str:
        return scope_for(arguments.get("destination", ""))

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        settings = context.settings
        source = resolve_path(arguments["source"], must_exist=True, settings=settings)
        destination = resolve_path(arguments["destination"], for_write=True, settings=settings)
        destination.path.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(source.path) as archive:
            # Zip-slip guard: an entry may not escape the destination directory.
            for member in archive.namelist():
                target = (destination.path / member).resolve()
                try:
                    target.relative_to(destination.path.resolve())
                except ValueError:
                    return ToolResult.failure(
                        f"Das Archiv enthält einen Eintrag, der aus dem Zielordner ausbricht "
                        f"('{member}'). Ich habe nichts entpackt."
                    )
            archive.extractall(destination.path)
            count = len(archive.namelist())

        return ToolResult(
            content=f"{count} Eintrag/Einträge nach {destination.path} entpackt.",
            display={"summary": f"{count} Einträge entpackt", "path": str(destination.path)},
        )


FILE_TOOLS = [
    ListDirectoryTool(), ReadFileTool(), SearchFilesTool(), WriteFileTool(), PatchFileTool(),
    CreateDirectoryTool(), CopyFileTool(), MoveFileTool(), DeleteFileTool(),
    DeleteDirectoryTool(), ArchiveTool(), ExtractArchiveTool(),
]
