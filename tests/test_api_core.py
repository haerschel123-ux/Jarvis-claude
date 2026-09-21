"""System endpoints and the realtime event socket (Spec §77, §78, §83)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.events import EventType, event_bus


@pytest.fixture
def client(jarvis_home: Path) -> Iterator[TestClient]:
    """A TestClient running the real lifespan against the temporary JARVIS home."""
    from memory.database import db

    import app as app_module

    db.set_path(jarvis_home / "data" / "jarvis.db")
    event_bus.clear_history()
    with TestClient(app_module.app) as test_client:
        yield test_client


def test_health_reports_every_subsystem(client: TestClient) -> None:
    payload = client.get("/api/health?refresh=true").json()
    assert payload["status"] == "ok"
    assert payload["version"]
    checks = payload["health"]["checks"]
    for name in ("database", "disk_space", "git", "voice", "desktop_control", "credentials"):
        assert name in checks
    assert checks["database"]["ok"] is True
    # Missing optional subsystems are warnings, never hard errors (Spec §114).
    assert payload["health"]["errors"] == []


def test_status_exposes_the_specified_defaults(client: TestClient) -> None:
    payload = client.get("/api/status").json()
    assert payload["assistant"]["name"] == "JARVIS"
    assert payload["models"]["free_only"] is True
    assert payload["voice"]["mode"] == "OFF"
    assert payload["voice"]["microphone_active"] is False


def test_platform_report_is_honest_about_gaps(client: TestClient) -> None:
    """Unavailable capabilities must state a reason rather than silently claiming support."""
    payload = client.get("/api/platform").json()
    for name, capability in payload["capabilities"].items():
        assert isinstance(capability["available"], bool), name
        if not capability["available"]:
            assert capability["reason"], f"{name} is unavailable but gives no reason"


def test_api_404_stays_json_while_ui_routes_fall_back(client: TestClient) -> None:
    assert client.get("/api/definitely-not-here").status_code == 404
    assert client.get("/api/definitely-not-here").json()["detail"] == "Not Found"
    assert client.get("/dashboard").status_code == 200


def test_event_socket_replays_history_then_streams(client: TestClient) -> None:
    event_bus.emit(EventType.TASK_CREATED, id=1, title="Serverprüfung")
    with client.websocket_connect("/api/events") as socket:
        ready = socket.receive_json()
        assert ready["type"] == "connection.ready"
        assert any(e["type"] == EventType.TASK_CREATED for e in ready["data"]["history"])

        event_bus.emit(EventType.TOOL_COMPLETED, tool="read_file", api_key="sk-or-v1-secret123456")
        message = socket.receive_json()
        assert message["type"] == EventType.TOOL_COMPLETED
        # The socket is a public surface: credentials must already be masked.
        assert "sk-or-v1-secret123456" not in str(message)


def test_event_socket_honours_the_type_filter(client: TestClient) -> None:
    with client.websocket_connect(f"/api/events?types={EventType.REMINDER_TRIGGERED}") as socket:
        socket.receive_json()  # connection.ready
        event_bus.emit(EventType.CHAT_DELTA, text="ignored")
        event_bus.emit(EventType.REMINDER_TRIGGERED, text="Bot prüfen")
        message = socket.receive_json()
        assert message["type"] == EventType.REMINDER_TRIGGERED
        assert message["data"]["text"] == "Bot prüfen"
