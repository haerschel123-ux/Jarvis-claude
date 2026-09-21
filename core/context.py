"""Token budget and context assembly (Spec §66).

Not everything goes into every prompt. Sections are added in the specification's priority
order and each one is truncated to the share of the budget it is allowed, so a huge file or a
long history can never push out the system policy or the user's actual question.

Token counts are **estimates**. JARVIS deliberately avoids a tokenizer dependency per model —
providers tokenise differently anyway — so the estimator errs on the high side and the budget
keeps a safety margin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from core.logging_setup import get_logger
from core.prompts import wrap_external, wrap_tool_result
from providers.base import Message

log = get_logger("context")

# Conservative characters-per-token ratio. German averages longer words than English, and
# underestimating tokens is the dangerous direction, so 3.2 is used rather than the usual 4.
CHARS_PER_TOKEN = 3.2

# Reserve part of the window for the answer and for provider overhead.
DEFAULT_OUTPUT_RESERVE = 2048
SAFETY_MARGIN = 0.92
FALLBACK_CONTEXT = 8192


class Priority(IntEnum):
    """Spec §66, highest first."""

    SYSTEM_POLICY = 1
    USER_GOAL = 2
    CURRENT_TASK = 3
    PROJECT_INSTRUCTIONS = 4
    MEMORY = 5
    FILE_CHUNKS = 6
    CONVERSATION = 7
    TOOL_OUTPUT = 8


# Share of the *optional* budget each priority may occupy. System policy and the user's goal
# are never trimmed, so they are absent here.
BUDGET_SHARE: dict[Priority, float] = {
    Priority.CURRENT_TASK: 0.10,
    Priority.PROJECT_INSTRUCTIONS: 0.10,
    Priority.MEMORY: 0.15,
    Priority.FILE_CHUNKS: 0.30,
    Priority.CONVERSATION: 0.25,
    Priority.TOOL_OUTPUT: 0.25,
}


def estimate_tokens(text: str) -> int:
    """Rough token estimate, biased upwards."""
    if not text:
        return 0
    return max(1, int(len(text) / CHARS_PER_TOKEN) + 1)


def estimate_messages(messages: list[Message]) -> int:
    """Include a per-message overhead for role and formatting tokens."""
    total = 0
    for message in messages:
        total += estimate_tokens(message.content) + 4
        for call in message.tool_calls:
            total += estimate_tokens(call.name) + estimate_tokens(str(call.arguments)) + 8
        # A base64 image costs far more than its text length suggests; providers bill by tiles,
        # so a flat, deliberately generous allowance is used.
        total += 800 * len(message.images)
    return total


@dataclass(slots=True)
class ContextSection:
    priority: Priority
    label: str
    content: str
    truncated: bool = False

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.content)


@dataclass(slots=True)
class BuiltContext:
    messages: list[Message]
    sections: list[ContextSection] = field(default_factory=list)
    used_tokens: int = 0
    budget: int = 0
    dropped: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "used_tokens": self.used_tokens,
            "budget": self.budget,
            "sections": [
                {"label": s.label, "priority": int(s.priority), "tokens": s.tokens,
                 "truncated": s.truncated}
                for s in self.sections
            ],
            "dropped": self.dropped,
        }


def truncate_to_tokens(text: str, max_tokens: int, *, from_end: bool = False) -> tuple[str, bool]:
    """Cut text to an estimated token budget, marking where content was removed."""
    if max_tokens <= 0:
        return "", bool(text)
    limit = int(max_tokens * CHARS_PER_TOKEN)
    if len(text) <= limit:
        return text, False
    notice = "\n[… gekürzt, um ins Kontextfenster zu passen …]\n"
    keep = max(limit - len(notice), 0)
    if from_end:
        return notice + text[-keep:], True
    return text[:keep] + notice, True


class ContextBuilder:
    """Assembles the message list for one model call."""

    def __init__(self, context_length: int | None, *, output_reserve: int = DEFAULT_OUTPUT_RESERVE) -> None:
        window = context_length or FALLBACK_CONTEXT
        self.budget = max(int(window * SAFETY_MARGIN) - output_reserve, 1024)
        self._sections: list[ContextSection] = []

    def add(self, priority: Priority, label: str, content: str) -> None:
        if content and content.strip():
            self._sections.append(ContextSection(priority, label, content.strip()))

    def add_memories(self, memories: list[dict[str, Any]]) -> None:
        if not memories:
            return
        lines = [
            f"- [{m.get('kind', 'fact')}] {m.get('subject', '')}: {m.get('content', '')}".strip()
            for m in memories
        ]
        self.add(
            Priority.MEMORY,
            "Gedächtnis",
            "Was du dir über den Benutzer gemerkt hast:\n" + "\n".join(lines),
        )

    def add_file_chunk(self, path: str, content: str) -> None:
        """File content is external data, so it is fenced (Spec §90)."""
        self.add(Priority.FILE_CHUNKS, f"Datei {path}", wrap_external(content, source=path))

    def add_tool_output(self, tool: str, content: str) -> None:
        self.add(Priority.TOOL_OUTPUT, f"Ergebnis {tool}", wrap_tool_result(tool, content))

    def build(
        self,
        *,
        system_prompt: str,
        user_message: str,
        history: list[Message] | None = None,
        user_images: list[str] | None = None,
    ) -> BuiltContext:
        """Assemble the final message list within the budget."""
        history = history or []
        result = BuiltContext(messages=[], budget=self.budget)

        # Never trimmed: the policy and the user's actual request.
        fixed_tokens = estimate_tokens(system_prompt) + estimate_tokens(user_message) + 8
        if user_images:
            fixed_tokens += 800 * len(user_images)
        optional_budget = max(self.budget - fixed_tokens, 0)

        # The conversation share is set aside before sections are laid out, so a large file or
        # a verbose tool result can never consume the history's budget.
        conversation_reserve = int(optional_budget * BUDGET_SHARE[Priority.CONVERSATION])
        by_priority = sorted(self._sections, key=lambda s: int(s.priority))
        kept: list[ContextSection] = []
        remaining = optional_budget - conversation_reserve

        for section in by_priority:
            share = BUDGET_SHARE.get(section.priority, 0.1)
            allowance = min(int(optional_budget * share), remaining)
            if allowance <= 0:
                result.dropped.append(section.label)
                continue
            if section.tokens <= allowance:
                kept.append(section)
                remaining -= section.tokens
                continue
            # Conversation history keeps its *end*; everything else keeps its beginning.
            from_end = section.priority is Priority.CONVERSATION
            content, truncated = truncate_to_tokens(section.content, allowance, from_end=from_end)
            if not content:
                result.dropped.append(section.label)
                continue
            kept.append(ContextSection(section.priority, section.label, content, truncated))
            remaining -= estimate_tokens(content)

        # Whatever the sections did not use is handed back to the history, which benefits
        # most from extra room. Reserve plus leftovers can never exceed the optional budget.
        system_parts = [system_prompt]
        for section in kept:
            if section.priority is not Priority.CONVERSATION:
                system_parts.append(f"## {section.label}\n{section.content}")

        messages: list[Message] = [
            Message("system", "\n\n".join(system_parts), trust="system")
        ]

        history_budget = conversation_reserve + remaining
        messages.extend(_fit_history(history, history_budget))
        messages.append(
            Message("user", user_message, images=list(user_images or []), trust="user")
        )

        result.messages = messages
        result.sections = kept
        result.used_tokens = estimate_messages(messages)
        if result.used_tokens > self.budget:
            log.warning(
                "Kontext über Budget: %d von %d Token", result.used_tokens, self.budget
            )
        return result


def _fit_history(history: list[Message], budget: int) -> list[Message]:
    """Keep the most recent turns that fit, without splitting a tool call from its result."""
    if budget <= 0 or not history:
        return []
    kept: list[Message] = []
    used = 0
    for message in reversed(history):
        cost = estimate_messages([message])
        if used + cost > budget:
            break
        kept.append(message)
        used += cost
    kept.reverse()

    # A tool result whose originating assistant turn was dropped confuses every provider, so
    # leading orphans are removed.
    while kept and kept[0].role == "tool":
        kept.pop(0)
    return kept
