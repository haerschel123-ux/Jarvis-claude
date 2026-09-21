"""Conversation and message persistence.

Messages keep their **trust** marker (Spec §90) and their provenance (model, agent, tool
calls, citations, usage), so a reloaded conversation reconstructs exactly the same context
the model originally saw — including which parts were untrusted external data.
"""

from __future__ import annotations

import json
from typing import Any

from core.logging_setup import get_logger
from core.redaction import redact_text
from memory.database import Database, db
from providers.base import Message, ToolCall

log = get_logger("conversations")

TITLE_MAX = 60


class ConversationStore:
    def __init__(self, database: Database | None = None) -> None:
        self._db = database or db

    # --- conversations --------------------------------------------------------------

    async def create(
        self,
        title: str = "",
        *,
        project_id: int | None = None,
        source: str = "text",
        model_id: str | None = None,
    ) -> int:
        return await self._db.execute(
            """
            INSERT INTO conversations (title, project_id, source, model_id)
            VALUES (?, ?, ?, ?)
            """,
            (title or "Neue Unterhaltung", project_id, source, model_id),
        )

    async def get(self, conversation_id: int) -> dict[str, Any] | None:
        return await self._db.fetch_one(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        )

    async def ensure(self, conversation_id: int | None, **kwargs: Any) -> int:
        """Return an existing conversation id, or create one."""
        if conversation_id is not None and await self.get(conversation_id) is not None:
            return conversation_id
        return await self.create(**kwargs)

    async def list(
        self, limit: int = 50, *, project_id: int | None = None, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if project_id is not None:
            clauses.append("c.project_id = ?")
            params.append(project_id)
        if not include_archived:
            clauses.append("c.archived = 0")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        return await self._db.fetch_all(
            f"""
            SELECT c.*,
                   (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count,
                   (SELECT m.content FROM messages m
                     WHERE m.conversation_id = c.id AND m.role = 'user'
                     ORDER BY m.id DESC LIMIT 1) AS last_user_message
            FROM conversations c
            {where}
            ORDER BY c.pinned DESC, c.updated_at DESC
            LIMIT ?
            """,
            params,
        )

    async def rename(self, conversation_id: int, title: str) -> None:
        await self._db.execute(
            "UPDATE conversations SET title = ?, updated_at = datetime('now') WHERE id = ?",
            (title[:TITLE_MAX], conversation_id),
        )

    async def set_flag(self, conversation_id: int, field: str, value: bool) -> None:
        if field not in {"pinned", "archived"}:
            raise ValueError(f"unsupported flag: {field}")
        await self._db.execute(
            f"UPDATE conversations SET {field} = ?, updated_at = datetime('now') WHERE id = ?",
            (int(value), conversation_id),
        )

    async def delete(self, conversation_id: int) -> None:
        await self._db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))

    async def auto_title(self, conversation_id: int, first_message: str) -> str:
        """Derive a readable title from the first message, without spending a model call."""
        cleaned = " ".join(redact_text(first_message).split())
        title = cleaned[:TITLE_MAX].rstrip()
        if len(cleaned) > TITLE_MAX:
            title = title.rsplit(" ", 1)[0] + " …"
        title = title or "Neue Unterhaltung"
        await self.rename(conversation_id, title)
        return title

    # --- messages -------------------------------------------------------------------

    async def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        *,
        trust: str = "user",
        model_id: str | None = None,
        agent: str | None = None,
        tool_calls: list[ToolCall] | None = None,
        tool_call_id: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        citations: list[dict[str, Any]] | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        cost_usd: float | None = None,
        error: str | None = None,
    ) -> int:
        message_id = await self._db.execute(
            """
            INSERT INTO messages
                (conversation_id, role, content, trust, model_id, agent, tool_calls,
                 attachments, citations, prompt_tokens, completion_tokens, cost_usd, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                role,
                content,
                trust,
                model_id,
                agent,
                json.dumps([c.to_dict() for c in tool_calls], ensure_ascii=False) if tool_calls else None,
                json.dumps(attachments, ensure_ascii=False) if attachments else None,
                json.dumps(citations, ensure_ascii=False) if citations else None,
                prompt_tokens,
                completion_tokens,
                cost_usd,
                error,
            ),
        )
        if tool_call_id:
            await self._db.execute(
                "UPDATE messages SET tool_calls = ? WHERE id = ?",
                (json.dumps({"tool_call_id": tool_call_id}, ensure_ascii=False), message_id),
            )
        await self._db.execute(
            "UPDATE conversations SET updated_at = datetime('now') WHERE id = ?",
            (conversation_id,),
        )
        return message_id

    async def messages(self, conversation_id: int, limit: int = 200) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
            (conversation_id, limit),
        )
        rows.reverse()
        return [_decode(row) for row in rows]

    async def history(self, conversation_id: int, limit: int = 60) -> list[Message]:
        """Rebuild the provider-level message list, preserving trust markers."""
        result: list[Message] = []
        for row in await self.messages(conversation_id, limit):
            if row["role"] not in ("user", "assistant", "tool"):
                continue
            if row.get("error"):
                continue  # a failed turn is not part of the conversation the model saw
            calls = [
                ToolCall(c.get("id", ""), c.get("name", ""), c.get("arguments", {}))
                for c in (row.get("tool_calls") or [])
                if isinstance(c, dict) and c.get("name")
            ]
            result.append(
                Message(
                    role=row["role"],
                    content=row["content"] or "",
                    tool_calls=calls,
                    trust=row.get("trust") or "user",
                )
            )
        return result

    async def delete_message(self, message_id: int) -> None:
        await self._db.execute("DELETE FROM messages WHERE id = ?", (message_id,))

    async def truncate_after(self, conversation_id: int, message_id: int) -> int:
        """Drop everything after a message — used by "edit and resend" in the UI."""
        return await self._db.execute(
            "DELETE FROM messages WHERE conversation_id = ? AND id > ?",
            (conversation_id, message_id),
        )

    async def usage_summary(self, conversation_id: int) -> dict[str, Any]:
        row = await self._db.fetch_one(
            """
            SELECT COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(cost_usd), 0)          AS cost_usd,
                   COUNT(*)                            AS messages
            FROM messages WHERE conversation_id = ?
            """,
            (conversation_id,),
        )
        return row or {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0, "messages": 0}


def _decode(row: dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    for field in ("tool_calls", "attachments", "citations"):
        raw = data.get(field)
        if raw:
            try:
                data[field] = json.loads(raw)
            except json.JSONDecodeError:
                data[field] = []
        else:
            data[field] = []
    return data


conversations = ConversationStore()
