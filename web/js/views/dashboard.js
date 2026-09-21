/* Dashboard (Spec §57): the orb, health, providers, capabilities and quick actions. */

import { api } from "../api.js";
import { el, formatBytes, icon, mount } from "../dom.js";
import { state } from "../state.js";
import { createOrb, emptyState, sectionCard, setOrbState, toast } from "../ui.js";

export async function renderDashboard(container) {
  const scroll = el("div", { class: "view__scroll" });
  mount(container,
    el("div", { class: "view__header" },
      el("div", {},
        el("h2", { class: "view__title" }, "Übersicht"),
        el("div", { class: "view__subtitle" }, "Zustand, Anbieter und Fähigkeiten dieses Systems"),
      ),
      el("button", { class: "btn btn--sm", onClick: () => renderDashboard(container) },
        icon("refresh", 14), "Aktualisieren"),
    ),
    scroll,
  );

  const orb = createOrb({ size: "lg", withLabel: true });
  setOrbState(orb, state.assistantState);
  scroll.append(el("div", { class: "orb-stage" }, orb,
    el("div", { class: "orb-stage__caption" },
      el("div", { class: "orb-stage__state", id: "dash-state" },
        `${state.settings?.assistant?.name ?? "JARVIS"} · ${state.assistantState}`),
      el("div", { class: "orb-stage__detail", id: "dash-detail" }, state.assistantDetail || ""),
    ),
  ));

  const grid = el("div", { class: "grid" });
  scroll.append(grid);
  grid.append(loadingCard("Systemzustand"), loadingCard("Anbieter"), loadingCard("Fähigkeiten"));

  const [health, providers, platform, tools] = await Promise.allSettled([
    api.health(true), api.providers(), api.platform(), api.tools(),
  ]);

  mount(grid,
    healthCard(health),
    providersCard(providers),
    capabilitiesCard(platform),
    hardwareCard(platform),
    toolsCard(tools),
    securityCard(),
  );
}

function loadingCard(title) {
  return el("section", { class: "card" },
    el("h3", { class: "card__title" }, title),
    el("div", { class: "skeleton", style: { height: "72px" } }, "lädt"),
  );
}

function healthCard(result) {
  if (result.status !== "fulfilled") {
    return sectionCard("Systemzustand", el("div", { class: "pill pill--danger" },
      "Zustand konnte nicht geladen werden"));
  }
  const health = result.value.health ?? {};
  const checks = Object.entries(health.checks ?? {});
  const overall = health.overall ?? "unknown";
  const pillClass = overall === "ok" ? "pill--ok" : overall === "warn" ? "pill--warn" : "pill--danger";

  return sectionCard("Systemzustand",
    el("div", { class: "spread", style: { marginBottom: "12px" } },
      el("span", { class: `pill ${pillClass}` }, el("span", { class: "dot" }),
        overall === "ok" ? "Alles in Ordnung" : overall === "warn" ? "Mit Einschränkungen" : "Problem"),
      el("span", { class: "tiny muted" }, `Version ${result.value.version}`),
    ),
    el("div", { class: "list" }, ...checks.map(([name, check]) =>
      el("div", { class: "row small", style: { gap: "8px" } },
        el("span", {
          class: "dot",
          style: { color: `var(--${check.status === "ok" ? "ok" : check.status === "warn" ? "warn" : "danger"})` },
        }),
        el("span", { style: { minWidth: "112px" } }, name),
        el("span", { class: "muted truncate grow", title: check.message }, check.message || "—"),
      ))),
  );
}

function providersCard(result) {
  if (result.status !== "fulfilled") {
    return sectionCard("Anbieter", el("div", { class: "muted small" }, "Nicht verfügbar"));
  }
  const providers = result.value.providers ?? [];
  if (!providers.length) {
    return sectionCard("Anbieter",
      emptyState("bolt", "Kein Anbieter aktiv",
        "Aktiviere OpenRouter oder Ollama in den Einstellungen."));
  }
  return sectionCard("KI-Anbieter",
    el("div", { class: "list" }, ...providers.map((provider) =>
      el("div", { class: "list-item" },
        el("span", {
          class: "dot",
          style: { color: provider.available ? "var(--ok)" : "var(--warn)" },
        }),
        el("div", { class: "grow", style: { minWidth: 0 } },
          el("div", {}, provider.label),
          el("div", { class: "tiny muted truncate", title: provider.reason }, provider.reason),
        ),
        el("span", { class: "pill tiny" }, `${provider.model_count} Modelle`),
      ))),
  );
}

function capabilitiesCard(result) {
  if (result.status !== "fulfilled") {
    return sectionCard("Fähigkeiten", el("div", { class: "muted small" }, "Nicht verfügbar"));
  }
  const capabilities = Object.entries(result.value.capabilities ?? {});
  const available = capabilities.filter(([, c]) => c.available).length;

  return sectionCard(`Fähigkeiten (${available}/${capabilities.length})`,
    el("div", { class: "list" }, ...capabilities.map(([name, capability]) =>
      el("div", { class: "row small", style: { gap: "8px", alignItems: "flex-start" } },
        icon(capability.available ? "check" : "x", 14,
          capability.available ? "" : "muted"),
        el("div", { class: "grow", style: { minWidth: 0 } },
          el("div", { style: { color: capability.available ? "var(--text)" : "var(--text-mute)" } },
            LABELS[name] ?? name),
          !capability.available && capability.reason
            ? el("div", { class: "tiny muted" }, capability.reason)
            : null,
        ),
      ))),
    el("p", { class: "tiny muted", style: { marginTop: "10px", marginBottom: 0 } },
      "Nicht verfügbare Funktionen werden nicht angeboten — JARVIS täuscht keine Fähigkeit vor."),
  );
}

const LABELS = {
  screen_capture: "Bildschirm erfassen",
  mouse_keyboard: "Maus und Tastatur",
  window_automation: "Fenstersteuerung",
  process_control: "Prozesse",
  speech_to_text: "Spracherkennung",
  microphone: "Mikrofon",
  wake_word: "Wake Word",
  text_to_speech: "Sprachausgabe",
  desktop_shell: "Desktop-Fenster",
  credential_store: "Schlüsselspeicher",
};

function hardwareCard(result) {
  if (result.status !== "fulfilled") return null;
  const hardware = result.value.hardware ?? {};
  const recommendations = result.value.recommendations ?? {};
  const rows = [
    ["Betriebssystem", `${hardware.os} ${hardware.os_release ?? ""}`.trim()],
    ["Prozessorkerne", hardware.cpu_count ?? "—"],
    ["Arbeitsspeicher", hardware.ram_total_gb ? `${hardware.ram_total_gb} GB` : "unbekannt"],
    ["Grafikkarte", hardware.gpu ?? "keine erkannt"],
    ["Freier Speicher", hardware.disk_free_gb ? `${hardware.disk_free_gb} GB` : "—"],
  ];
  return sectionCard("Hardware",
    el("dl", { class: "kv" }, ...rows.flatMap(([key, value]) =>
      [el("dt", {}, key), el("dd", {}, String(value))])),
    el("div", { class: "divider" }),
    el("div", { class: "tiny muted" }, "Empfehlung für lokale Modelle"),
    el("div", { class: "small" }, recommendations.ollama_class ?? "—"),
    el("div", { class: "tiny muted" },
      `Spracherkennung: ${recommendations.whisper_model ?? "—"} (${recommendations.stt_compute_type ?? ""})`),
  );
}

function toolsCard(result) {
  if (result.status !== "fulfilled") return null;
  const tools = result.value.tools ?? [];
  const byRisk = new Map();
  for (const tool of tools) {
    byRisk.set(tool.risk_level, (byRisk.get(tool.risk_level) ?? 0) + 1);
  }
  const sandbox = result.value.sandbox ?? {};
  return sectionCard("Werkzeuge",
    el("div", { class: "card__value" }, String(tools.filter((t) => t.available).length)),
    el("div", { class: "tiny muted", style: { marginBottom: "10px" } },
      `von ${tools.length} registriert`),
    el("div", { class: "row wrap" }, ...[...byRisk.entries()].map(([risk, count]) =>
      el("span", { class: "pill tiny" }, `${risk}: ${count}`))),
    el("div", { class: "divider" }),
    el("div", { class: "tiny muted" }, "Freigegebene Ordner"),
    sandbox.configured
      ? el("div", { class: "small mono", style: { overflowWrap: "anywhere" } },
          (sandbox.trusted_folders ?? []).join(", "))
      : el("span", { class: "pill pill--warn tiny" }, "keiner — Dateizugriff gesperrt"),
  );
}

function securityCard() {
  const settings = state.settings;
  if (!settings) return null;
  const level = settings.assistant?.autonomy_level ?? 2;
  const levelNames = ["Nur lesen", "Alles fragen", "Riskantes fragen", "Vertrauenswürdig auto", "Voll autonom"];
  const permissions = settings.permissions ?? {};
  const counts = { ALLOW: 0, ASK: 0, DENY: 0 };
  for (const value of Object.values(permissions)) counts[value] = (counts[value] ?? 0) + 1;

  return sectionCard("Autonomie und Rechte",
    el("div", { class: "card__value" }, `Stufe ${level}`),
    el("div", { class: "tiny muted", style: { marginBottom: "12px" } }, levelNames[level] ?? ""),
    el("div", { class: "meter" },
      el("div", { class: "meter__fill", style: { width: `${(level / 4) * 100}%` } })),
    el("div", { class: "row wrap", style: { marginTop: "12px" } },
      el("span", { class: "pill pill--ok tiny" }, `${counts.ALLOW} erlaubt`),
      el("span", { class: "pill pill--warn tiny" }, `${counts.ASK} fragt nach`),
      el("span", { class: "pill pill--danger tiny" }, `${counts.DENY} gesperrt`),
    ),
    el("div", { class: "divider" }),
    el("div", { class: "row wrap" },
      el("span", { class: `pill tiny ${settings.models?.free_only ? "pill--ok" : "pill--warn"}` },
        settings.models?.free_only ? "Nur kostenlose Modelle" : "Kostenpflichtige erlaubt"),
      el("span", { class: "pill tiny" }, `Memory: ${settings.memory?.mode ?? "—"}`),
      el("span", { class: "pill tiny" }, `Router: ${settings.models?.router_mode ?? "—"}`),
    ),
  );
}

export { toast, formatBytes };
