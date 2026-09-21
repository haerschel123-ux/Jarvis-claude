/* The activity / audit panel (Spec §58).

   Shows what JARVIS is doing, in plain language. It deliberately never renders API keys or
   hidden system prompts — the backend already redacts event payloads, and this layer shows
   only the fields it understands. */

import { api } from "../js/api.js";
import { clear, el, formatDateTime, formatTime, icon } from "./dom.js";
import { events } from "./events.js";
import { state } from "./state.js";
import { emptyState } from "./ui.js";

const MAX_EVENTS = 200;
let tab = "events";
const feed = [];

/** Turn a backend event into a short German line. Unknown types are shown verbatim. */
function describe(event) {
  const data = event.data ?? {};
  const map = {
    "assistant.listening": ["accent", "Hört zu"],
    "assistant.recognizing": ["accent", "Erkennt Sprache"],
    "assistant.thinking": ["accent", "Denkt nach"],
    "assistant.acting": ["accent", "Führt aus"],
    "assistant.speaking": ["ok", "Spricht"],
    "assistant.error": ["error", "Fehler"],
    "assistant.idle": ["", "Bereit"],
    "chat.started": ["accent", "Anfrage gestartet"],
    "chat.completed": ["ok", `Antwort abgeschlossen${data.duration_ms ? ` (${data.duration_ms} ms)` : ""}`],
    "chat.failed": ["error", `Anfrage fehlgeschlagen: ${data.error ?? ""}`],
    "models.refreshed": ["ok", `Modellkatalog aktualisiert (${data.total ?? 0})`],
    "settings.updated": ["", `Einstellungen geändert: ${(data.changed ?? []).join(", ")}`],
    "health.updated": [
      data.overall === "ok" ? "ok" : data.overall === "warn" ? "warn" : "error",
      `Systemzustand: ${data.overall ?? "?"}`,
    ],
    "emergency.stop": [data.engaged ? "error" : "ok",
      data.engaged ? `NOT-STOPP: ${data.reason ?? ""}` : "Not-Stopp aufgehoben"],
    "tool.requested": ["accent", `Werkzeug angefragt: ${data.tool ?? ""}`],
    "tool.started": ["accent", `Werkzeug läuft: ${data.tool ?? ""}`],
    "tool.completed": ["ok",
      `Werkzeug fertig: ${data.tool ?? ""}${data.duration_ms ? ` (${data.duration_ms} ms)` : ""}`],
    "tool.failed": ["error", `Werkzeug fehlgeschlagen: ${data.tool ?? ""}`],
    "tool.denied": ["warn", `Abgelehnt: ${data.tool ?? ""}`],
    "tool.awaiting_permission": ["warn", `Wartet auf Bestätigung: ${data.tool ?? ""}`],
    "task.created": ["accent", `Aufgabe angelegt: ${data.title ?? ""}`],
    "task.completed": ["ok", `Aufgabe erledigt: ${data.title ?? ""}`],
    "task.failed": ["error", `Aufgabe fehlgeschlagen: ${data.title ?? ""}`],
    "memory.created": ["ok", `Gemerkt: ${data.subject ?? ""}`],
    "reminder.triggered": ["accent", `Erinnerung: ${data.text ?? ""}`],
    "integration.status": ["", `${data.name ?? "Integration"}: ${data.status ?? ""}`],
    "voice.wake_detected": ["accent", "Wake Word erkannt"],
    "voice.transcript": ["", `Verstanden: ${data.text ?? ""}`],
    "notification": ["accent", data.message ?? "Hinweis"],
    "proactive.message": ["accent", data.message ?? "Hinweis"],
  };
  const entry = map[event.type];
  if (entry) return { kind: entry[0], text: entry[1] };
  if (event.type.startsWith("local.")) return null;
  if (event.type === "chat.delta") return null;      // far too frequent to list
  return { kind: "", text: event.type };
}

/** Append an entry, collapsing an immediate repeat into a counter.
    The assistant legitimately reports "thinking" more than once per turn (routing, then
    generating); listing it twice is noise rather than information. */
function push(entry) {
  const last = feed.at(-1);
  if (last && last.text === entry.text && last.kind === entry.kind) {
    last.count = (last.count ?? 1) + 1;
    last.time = entry.time;
    return;
  }
  feed.push(entry);
  if (feed.length > MAX_EVENTS) feed.splice(0, feed.length - MAX_EVENTS);
}

export function initActivity() {
  const body = document.getElementById("activity-body");

  document.querySelectorAll("[data-activity-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      tab = button.dataset.activityTab;
      document.querySelectorAll("[data-activity-tab]").forEach((other) => {
        other.setAttribute("aria-pressed", String(other === button));
      });
      render();
    });
  });

  events.on("local.history", ({ events: history }) => {
    feed.length = 0;
    for (const event of history ?? []) {
      const described = describe(event);
      if (described) push({ ...described, time: event.timestamp });
    }
    if (tab === "events") render();
  });

  events.onAny((event) => {
    const described = describe(event);
    if (!described) return;
    push({ ...described, time: event.timestamp });
    if (tab === "events") render();
  });

  render();
  return body;
}

export function render() {
  const body = document.getElementById("activity-body");
  if (!body) return;
  if (tab === "events") renderEvents(body);
  else if (tab === "tools") renderToolRuns(body);
  else renderHealth(body);
}

function renderEvents(body) {
  clear(body);
  if (!feed.length) {
    body.append(emptyState("bolt", "Noch keine Aktivität",
      "Hier erscheint jeder Schritt: Absicht, Modell, Werkzeug, Ergebnis."));
    return;
  }
  // Newest first, so the latest step is visible without scrolling.
  for (const entry of [...feed].reverse()) {
    body.append(el("div", { class: `event${entry.kind ? ` event--${entry.kind}` : ""}` },
      el("span", { class: "event__time" }, formatTime(entry.time)),
      el("span", { class: "event__text" },
        el("b", {}, entry.text),
        entry.count > 1 ? el("span", { class: "tiny muted" }, ` ×${entry.count}`) : null,
      ),
    ));
  }
}

async function renderToolRuns(body) {
  clear(body);
  body.append(el("div", { class: "skeleton", style: { height: "60px" } }, "lädt"));
  try {
    const { runs } = await api.toolRuns(30);
    clear(body);
    if (!runs.length) {
      body.append(emptyState("bolt", "Noch nichts ausgeführt", ""));
      return;
    }
    for (const run of runs) {
      body.append(el("div", { class: "event" },
        el("span", {
          class: "dot",
          style: {
            marginTop: "5px",
            color: run.status === "success" ? "var(--ok)"
              : run.status === "denied" ? "var(--warn)" : "var(--danger)",
          },
        }),
        el("span", { class: "event__text" },
          el("b", { class: "mono" }, run.tool),
          el("div", { class: "tiny muted truncate" }, run.result_summary || run.error || run.status),
          el("div", { class: "tiny muted" }, formatDateTime(run.created_at)),
        ),
      ));
    }
  } catch (error) {
    clear(body);
    body.append(emptyState("warn", "Nicht verfügbar", error.message));
  }
}

async function renderHealth(body) {
  clear(body);
  body.append(el("div", { class: "skeleton", style: { height: "60px" } }, "lädt"));
  try {
    const data = await api.health(true);
    clear(body);
    const checks = Object.entries(data.health?.checks ?? {});
    body.append(el("div", { class: "row", style: { marginBottom: "10px" } },
      icon("shield", 15),
      el("span", { class: "small" }, `Version ${data.version}`),
      el("span", { class: "grow" }),
      el("span", { class: "tiny muted" }, `${Math.round(data.uptime_seconds)} s aktiv`),
    ));
    for (const [name, check] of checks) {
      body.append(el("div", { class: "event" },
        el("span", {
          class: "dot",
          style: {
            marginTop: "5px",
            color: check.status === "ok" ? "var(--ok)"
              : check.status === "warn" ? "var(--warn)" : "var(--danger)",
          },
        }),
        el("span", { class: "event__text" },
          el("b", {}, name),
          el("div", { class: "tiny muted" }, check.message || "—"),
        ),
      ));
    }
  } catch (error) {
    clear(body);
    body.append(emptyState("warn", "Nicht verfügbar", error.message));
  }
}

export { state };
