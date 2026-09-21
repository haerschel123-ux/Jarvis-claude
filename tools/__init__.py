"""Tool engine.

:func:`register_default_tools` is the single place where the built-in tools are registered,
so the app, the tests and any future plugin loader all see the same set.
"""

from __future__ import annotations

from core.logging_setup import get_logger
from tools.registry import ToolRegistry, registry

log = get_logger("tools")


def register_default_tools(target: ToolRegistry | None = None) -> ToolRegistry:
    """Register every built-in tool with its intent tags. Idempotent."""
    target = target or registry

    from tools.files import FILE_TOOLS
    from tools.git_tools import GIT_TOOLS
    from tools.memory_tools import MEMORY_TOOLS, SCHEDULE_TOOLS
    from tools.terminal import TERMINAL_TOOLS
    from tools.web import WEB_TOOLS

    for tool in FILE_TOOLS:
        target.register(tool, tags={"files"})
    for tool in TERMINAL_TOOLS:
        target.register(tool, tags={"terminal", "system"})
    for tool in WEB_TOOLS:
        target.register(tool, tags={"web"})
    for tool in GIT_TOOLS:
        target.register(tool, tags={"git", "files"})
    for tool in MEMORY_TOOLS:
        target.register(tool, tags={"memory"})
    for tool in SCHEDULE_TOOLS:
        target.register(tool, tags={"schedule"})

    unavailable = [t.name for t in target.all(include_unavailable=True) if not t.available]
    log.info(
        "%d Werkzeuge registriert%s",
        len(target.names()),
        f" ({len(unavailable)} auf diesem System nicht verfügbar: {', '.join(unavailable)})"
        if unavailable else "",
    )
    return target


__all__ = ["ToolRegistry", "register_default_tools", "registry"]
