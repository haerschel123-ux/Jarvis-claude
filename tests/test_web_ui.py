"""The web UI is served correctly and its assets are self-contained (Spec §74).

These are static checks, not browser tests: they guard the contract between the backend and
the front-end (which files must exist, which routes must serve them) and the offline
requirement (no external fetches).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.paths import PACKAGE_ROOT

WEB = PACKAGE_ROOT / "web"


@pytest.fixture
def client(jarvis_home: Path) -> Iterator[TestClient]:
    import app as app_module
    from memory.database import db

    db.set_path(jarvis_home / "data" / "jarvis.db")
    with TestClient(app_module.app) as test_client:
        yield test_client


def test_index_is_served(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "<title>JARVIS</title>" in response.text
    assert 'lang="de"' in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/css/tokens.css", "/css/base.css", "/css/layout.css", "/css/components.css",
        "/css/orb.css", "/css/animations.css",
        "/js/app.js", "/js/api.js", "/js/dom.js", "/js/events.js", "/js/state.js",
        "/js/ui.js", "/js/router.js", "/js/activity.js",
        "/js/views/chat.js", "/js/views/dashboard.js", "/js/views/models.js",
        "/js/views/tools.js", "/js/views/settings.js", "/js/views/placeholder.js",
        "/manifest.json", "/sw.js", "/assets/icons/icon.svg",
    ],
)
def test_every_referenced_asset_exists(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 200, f"{path} is missing"


def test_manifest_is_valid_and_its_icons_exist(client: TestClient) -> None:
    manifest = client.get("/manifest.json").json()
    assert manifest["name"] and manifest["start_url"] == "/"
    assert manifest["display"] == "standalone"
    for entry in manifest["icons"]:
        assert client.get(entry["src"]).status_code == 200, entry["src"]


def test_ui_fetches_nothing_from_the_internet() -> None:
    """Spec §74/§84: the interface must work offline, so no external asset is referenced."""
    offenders: list[str] = []
    for file in [*WEB.rglob("*.html"), *WEB.rglob("*.css"), *WEB.rglob("*.js")]:
        text = file.read_text(encoding="utf-8")
        for match in re.finditer(r"""["'(](https?:)?//([\w.-]+)""", text):
            host = match.group(2)
            # Namespace URLs in inline SVG are identifiers, not network requests, and a
            # loopback address in a form placeholder is an example for the user to replace.
            if host in ("www.w3.org", "127.0.0.1", "localhost"):
                continue
            offenders.append(f"{file.relative_to(WEB)}: {host}")
    assert not offenders, f"external references found: {offenders}"


def test_service_worker_never_caches_api_responses() -> None:
    """A cached model list or health report would be worse than an honest error."""
    source = (WEB / "sw.js").read_text(encoding="utf-8")
    assert "/api/" in source
    assert "startsWith" in source


def test_client_side_routes_fall_back_to_the_shell(client: TestClient) -> None:
    for route in ("/dashboard", "/chat", "/settings"):
        response = client.get(route)
        assert response.status_code == 200
        assert "<title>JARVIS</title>" in response.text


def test_every_navigation_entry_has_a_handler() -> None:
    """A menu item without a registered view would render an empty page."""
    router = (WEB / "js" / "router.js").read_text(encoding="utf-8")
    app_source = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    keys = set(re.findall(r'\{ key: "(\w+)"', router))
    registered = set(re.findall(r'onRoute\("(\w+)"', app_source))
    placeholders = set(re.findall(r'"(\w+)"', app_source.split("for (const key of [")[1].split("]")[0]))
    assert keys <= (registered | placeholders), f"no handler for: {keys - registered - placeholders}"


def test_reduced_motion_is_respected() -> None:
    """Motion is decoration; it must be switchable off (accessibility)."""
    animations = (WEB / "css" / "animations.css").read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in animations
    assert 'data-reduced-motion="true"' in animations


def test_orb_defines_all_eight_states() -> None:
    """Spec §56: the orb has exactly these states and each must be styled."""
    css = (WEB / "css" / "orb.css").read_text(encoding="utf-8")
    for state in ("IDLE", "LISTENING", "RECOGNIZING", "THINKING", "ACTING", "SPEAKING",
                  "ERROR", "PAUSED"):
        assert f'data-state="{state}"' in css, f"orb state {state} is not styled"

    ui = (WEB / "js" / "ui.js").read_text(encoding="utf-8")
    labels = set(re.findall(r"^\s{2}(\w+): \{ label:", ui, re.M))
    assert labels == {"IDLE", "LISTENING", "RECOGNIZING", "THINKING", "ACTING", "SPEAKING",
                      "ERROR", "PAUSED"}


def test_model_output_is_never_inserted_as_raw_html() -> None:
    """Spec §90: a model response must not be able to inject markup."""
    chat = (WEB / "js" / "views" / "chat.js").read_text(encoding="utf-8")
    assert "innerHTML" not in chat, "chat view must not assign innerHTML directly"
    dom = (WEB / "js" / "dom.js").read_text(encoding="utf-8")
    assert "escapeHtml" in dom
    # renderMarkdown must escape before it produces any markup.
    body = dom.split("function inlineMarkdown")[1]
    assert "escapeHtml(text)" in body.split("\n")[1] or "escapeHtml(text)" in body[:200]


def test_manifest_and_index_agree_on_the_icon() -> None:
    index = (WEB / "index.html").read_text(encoding="utf-8")
    manifest = json.loads((WEB / "manifest.json").read_text(encoding="utf-8"))
    assert 'rel="manifest"' in index
    assert any(entry["src"].endswith("icon.svg") for entry in manifest["icons"])
