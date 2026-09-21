/* Application entry point: wiring, live state and the global controls. */

import { api } from "./api.js";
import { initActivity } from "./activity.js";
import { el, icon } from "./dom.js";
import { events } from "./events.js";
import { buildNav, dispatch, navigate, onRoute, startRouter } from "./router.js";
import { setState, state, subscribe } from "./state.js";
import { confirmDialog, permissionDialog, setOrbState, toast } from "./ui.js";
import { animateOrbs, newConversation, openConversation, renderChat, renderMessages } from "./views/chat.js";
import { renderDashboard } from "./views/dashboard.js";
import { renderAutomations } from "./views/automations.js";
import { renderMemory } from "./views/memory.js";
import { renderModels } from "./views/models.js";
import { renderPlaceholder } from "./views/placeholder.js";
import { applyAppearance, renderSettings } from "./views/settings.js";
import { renderTasks } from "./views/tasks.js";
import { renderTools } from "./views/tools.js";

const openPermissionDialogs = new Map();

async function boot() {
  buildNav(icon, (route, options) => {
    if (route === "chat" && options?.fresh) newConversation();
    navigate(route);
  });

  onRoute("dashboard", renderDashboard);
  onRoute("chat", renderChat);
  onRoute("models", renderModels);
  onRoute("tools", renderTools);
  onRoute("settings", renderSettings);
  onRoute("memory", renderMemory);
  onRoute("tasks", renderTasks);
  onRoute("automations", renderAutomations);
  for (const key of ["projects", "integrations"]) {
    onRoute(key, (container) => renderPlaceholder(container, key));
  }

  wireGlobalControls();
  initActivity();
  wireEvents();
  events.connect();
  startRouter();

  await Promise.allSettled([loadSettings(), refreshStatus(), checkEmergency(), catchUpPermissions()]);
  registerServiceWorker();
}

/* --- initial data ------------------------------------------------------------------------ */

async function loadSettings() {
  try {
    const data = await api.settings();
    setState({ settings: data.settings });
    applyAppearance(data.settings);
    if (!data.settings.first_run_completed) offerSetup();
  } catch (error) {
    toast(`Einstellungen konnten nicht geladen werden: ${error.message}`, { kind: "error" });
  }
}

async function refreshStatus() {
  try {
    const status = await api.status();
    setState({
      status,
      assistantState: status.assistant?.state ?? "IDLE",
      activeModel: state.activeModel,
    });
    updateTopbar();
  } catch {
    /* the connection indicator already reports this */
  }
}

async function checkEmergency() {
  try {
    const result = await api.emergencyState();
    setState({ emergencyEngaged: Boolean(result.engaged) });
    updateTopbar();
  } catch {
    /* ignored: the emergency state is also pushed over the event stream */
  }
}

/** After a reload there may already be a confirmation waiting; show it immediately. */
async function catchUpPermissions() {
  try {
    const { pending } = await api.pendingPermissions();
    for (const entry of pending ?? []) showPermissionDialog(entry.id, entry.request);
  } catch {
    /* ignored */
  }
}

function offerSetup() {
  toast(
    "Ersteinrichtung: hinterlege einen Anbieter und gib mindestens einen Ordner frei, " +
    "damit ich arbeiten kann.",
    { kind: "info", timeout: 12000, action: { label: "Einstellungen", onClick: () => navigate("settings") } },
  );
}

/* --- live events --------------------------------------------------------------------------- */

function wireEvents() {
  const stateEvents = {
    "assistant.idle": "IDLE",
    "assistant.listening": "LISTENING",
    "assistant.recognizing": "RECOGNIZING",
    "assistant.thinking": "THINKING",
    "assistant.acting": "ACTING",
    "assistant.speaking": "SPEAKING",
    "assistant.error": "ERROR",
    "assistant.paused": "PAUSED",
  };
  for (const [type, value] of Object.entries(stateEvents)) {
    events.on(type, () => {
      setState({ assistantState: value });
      updateOrbs();
      updateTopbar();
    });
  }

  events.on("local.connection", ({ connected }) => {
    setState({ connected });
    updateTopbar();
    if (!connected) {
      toast("Verbindung zu JARVIS verloren. Ich versuche es weiter.",
            { kind: "warn", timeout: 4000 });
    }
  });

  events.on("tool.awaiting_permission", (data) => {
    const { id, ...request } = data;
    showPermissionDialog(id, request);
  });

  events.on("emergency.stop", (data) => {
    setState({ emergencyEngaged: Boolean(data.engaged) });
    updateTopbar();
    for (const [, handle] of openPermissionDialogs) handle.close("deny");
    openPermissionDialogs.clear();
    toast(data.engaged ? `NOT-STOPP aktiv: ${data.reason ?? ""}` : "Not-Stopp aufgehoben.",
          { kind: data.engaged ? "error" : "ok" });
  });

  events.on("settings.updated", () => loadSettings());
  events.on("health.updated", () => { if (location.hash.includes("dashboard")) dispatch(); });

  events.on("notification", (data) => toast(data.message ?? "Hinweis", { kind: data.kind ?? "info" }));
  events.on("proactive.message", (data) =>
    toast(data.message ?? "", { kind: "info", timeout: 9000 }));
  events.on("reminder.triggered", (data) =>
    toast(`Erinnerung: ${data.text ?? ""}`, { kind: "info", timeout: 0 }));

  window.addEventListener("jarvis:conversations-changed", () => {
    if (location.hash.includes("chat")) return;   // the chat view refreshes itself
  });
}

function showPermissionDialog(id, request) {
  if (!id || openPermissionDialogs.has(id)) return;
  const handle = permissionDialog(request, {
    onAnswer: async (answer) => {
      openPermissionDialogs.delete(id);
      try {
        await api.answerPermission(id, answer);
      } catch (error) {
        // A confirmation that timed out counts as denied on the server; say so plainly.
        toast(`Antwort konnte nicht übermittelt werden: ${error.message}`, { kind: "warn" });
      }
    },
  });
  openPermissionDialogs.set(id, handle);
}

/* --- topbar and orbs -------------------------------------------------------------------------- */

const STATE_LABELS = {
  IDLE: "Bereit", LISTENING: "Hört zu", RECOGNIZING: "Erkennt", THINKING: "Denkt nach",
  ACTING: "Führt aus", SPEAKING: "Spricht", ERROR: "Fehler", PAUSED: "Pausiert",
};

const STATE_COLORS = {
  IDLE: "var(--text-mute)", LISTENING: "var(--accent)", RECOGNIZING: "var(--accent)",
  THINKING: "var(--violet)", ACTING: "var(--warn)", SPEAKING: "var(--ok)",
  ERROR: "var(--danger)", PAUSED: "var(--text-mute)",
};

function updateTopbar() {
  const pill = document.getElementById("status-pill");
  const text = document.getElementById("status-text");
  if (pill && text) {
    const label = state.connected === false ? "Getrennt" : STATE_LABELS[state.assistantState] ?? "—";
    text.textContent = label;
    const dot = pill.querySelector(".dot");
    if (dot) {
      dot.style.color = state.connected === false ? "var(--danger)"
        : STATE_COLORS[state.assistantState] ?? "var(--text-mute)";
      dot.classList.toggle("dot--live", state.assistantState !== "IDLE");
    }
  }

  const modelPill = document.getElementById("model-pill");
  if (modelPill) {
    modelPill.textContent = state.activeModel ?? "";
    modelPill.classList.toggle("hidden", !state.activeModel);
  }

  const taskPill = document.getElementById("task-pill");
  if (taskPill) {
    taskPill.textContent = state.activeTask ?? "";
    taskPill.classList.toggle("hidden", !state.activeTask);
  }

  document.getElementById("stopped-pill")?.classList.toggle("hidden", !state.emergencyEngaged);

  const dashState = document.getElementById("dash-state");
  if (dashState) {
    dashState.textContent =
      `${state.settings?.assistant?.name ?? "JARVIS"} · ${STATE_LABELS[state.assistantState] ?? ""}`;
  }
}

function updateOrbs() {
  for (const orb of document.querySelectorAll(".orb")) setOrbState(orb, state.assistantState);
  animateOrbs();
}

/* --- global controls ---------------------------------------------------------------------------- */

function wireGlobalControls() {
  document.getElementById("nav-toggle")?.addEventListener("click", () => {
    const app = document.getElementById("app");
    if (app.dataset.nav === "open") app.removeAttribute("data-nav");
    else app.dataset.nav = "open";
    toggleScrim();
  });

  document.getElementById("activity-toggle")?.addEventListener("click", () => {
    const app = document.getElementById("app");
    if (window.innerWidth <= 1080) {
      app.dataset.activity = app.dataset.activity === "open" ? "visible" : "open";
    } else {
      app.dataset.activity = app.dataset.activity === "hidden" ? "visible" : "hidden";
    }
  });

  document.getElementById("emergency-btn")?.addEventListener("click", async () => {
    if (state.emergencyEngaged) {
      await api.emergencyRelease();
      return;
    }
    const confirmed = await confirmDialog(
      "Alles stoppen?",
      "Ich beende sofort die laufende Werkzeugkette, jede Automation und die Sprachausgabe. " +
      "Die Verbindung zum Modell bleibt bestehen, damit du weiter mit mir reden kannst.",
      { confirmLabel: "Jetzt stoppen", variant: "risk" },
    );
    if (confirmed) await api.emergencyStop("Über die Oberfläche ausgelöst");
  });

  // Ctrl+Shift+Space is the emergency stop shortcut: reachable with one hand and not bound
  // by Windows itself (Spec §19 — do not collide with system shortcuts).
  document.addEventListener("keydown", (event) => {
    if (event.ctrlKey && event.shiftKey && event.code === "Space") {
      event.preventDefault();
      if (!state.emergencyEngaged) {
        api.emergencyStop("Tastenkürzel Strg+Shift+Leertaste").catch(() => {});
      }
    }
    if (event.key === "Escape") {
      document.getElementById("app")?.removeAttribute("data-nav");
      toggleScrim();
    }
  });

  window.addEventListener("resize", () => {
    if (window.innerWidth > 760) document.getElementById("app")?.removeAttribute("data-nav");
    toggleScrim();
  });
}

function toggleScrim() {
  const app = document.getElementById("app");
  const existing = document.querySelector(".scrim");
  const wanted = app?.dataset.nav === "open" && window.innerWidth <= 760;
  if (wanted && !existing) {
    const scrim = el("div", {
      class: "scrim",
      onClick: () => { app.removeAttribute("data-nav"); toggleScrim(); },
    });
    app.append(scrim);
  } else if (!wanted && existing) {
    existing.remove();
  }
}

/* --- service worker ------------------------------------------------------------------------------- */

function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) return;
  // Only register over a secure context; browsers refuse it otherwise and log a confusing error.
  if (location.protocol !== "https:" && location.hostname !== "localhost" &&
      location.hostname !== "127.0.0.1") return;
  navigator.serviceWorker.register("/sw.js").catch(() => {
    /* the app works fine without offline caching */
  });
}

subscribe((_, keys) => {
  if (keys.includes("assistantState")) updateOrbs();
  if (keys.some((key) => ["messages", "streaming"].includes(key)) && location.hash.includes("chat")) {
    /* the chat view updates its own DOM incrementally */
  }
});

document.addEventListener("DOMContentLoaded", boot);

export { openConversation, renderMessages };
