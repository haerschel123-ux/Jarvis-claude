/* Settings (Spec §15, §16, §61, §62): assistant, models, providers, permissions, secrets.
   Secrets are only ever shown masked — the API never returns their value. */

import { api } from "../api.js";
import { el, icon, mount } from "../dom.js";
import { setState, state } from "../state.js";
import { fieldRow, sectionCard, segmented, switchControl, toast } from "../ui.js";

const AUTONOMY = [
  ["0", "Nur lesen", "Analysiert, verändert nichts."],
  ["1", "Alles fragen", "Vor jeder verändernden Aktion eine Rückfrage."],
  ["2", "Riskantes fragen", "Sichere Aktionen laufen, Änderungen werden bestätigt."],
  ["3", "Vertrauenswürdig", "Handelt in freigegebenen Bereichen selbstständig."],
  ["4", "Voll autonom", "Umfangreiche eigenständige Aktionen."],
];

const CAPABILITY_LABELS = {
  file_read: "Dateien lesen", file_write: "Dateien schreiben", file_delete: "Dateien löschen",
  terminal_read: "Terminal (lesend)", terminal_write: "Terminal (schreibend)",
  terminal_admin: "Terminal (Administrator)", mouse: "Maus", keyboard: "Tastatur",
  screen_capture: "Bildschirm erfassen", program_launch: "Programme starten",
  program_close: "Programme schließen", git_commit: "Git commit", git_push: "Git push",
  email_read: "E-Mail lesen", email_send: "E-Mail senden", calendar_read: "Kalender lesen",
  calendar_write: "Kalender schreiben", discord: "Discord", nitrado: "Nitrado",
  smart_home: "Smart Home", web_search: "Websuche", downloads: "Downloads",
};

const SECRET_LABELS = {
  openrouter_api_key: "OpenRouter API-Schlüssel",
  custom_openai_api_key: "Custom API-Schlüssel",
  github_token: "GitHub Token",
  discord_bot_token: "Discord Bot Token",
  nitrado_token: "Nitrado Token",
  home_assistant_token: "Home Assistant Token",
  google_client_secret: "Google Client Secret",
};

export async function renderSettings(container) {
  const scroll = el("div", { class: "view__scroll" });
  mount(container,
    el("div", { class: "view__header" },
      el("div", {},
        el("h2", { class: "view__title" }, "Einstellungen"),
        el("div", { class: "view__subtitle" },
          "Alles hier gilt sofort. Zugangsdaten werden nie im Klartext angezeigt."),
      ),
    ),
    scroll,
  );

  scroll.append(el("div", { class: "skeleton", style: { height: "150px" } }, "lädt"));

  const data = await api.settings();
  setState({ settings: data.settings });

  mount(scroll,
    assistantSection(data.settings),
    modelSection(data.settings),
    providerSection(data.settings, data.secrets, data.secret_backend),
    autonomySection(data.settings),
    permissionSection(data.settings),
    sandboxSection(data.settings),
    appearanceSection(data.settings),
  );
}

async function patch(path, value) {
  try {
    const result = await api.updateSettings(nest(path, value));
    setState({ settings: result.settings });
    applyAppearance(result.settings);
    return result.settings;
  } catch (error) {
    toast(error.message, { kind: "error" });
    throw error;
  }
}

function nest(path, value) {
  const parts = path.split(".");
  const root = {};
  let node = root;
  parts.forEach((part, index) => {
    if (index === parts.length - 1) node[part] = value;
    else { node[part] = {}; node = node[part]; }
  });
  return root;
}

function assistantSection(settings) {
  return sectionCard("Assistent",
    el("div", { class: "grid", style: { gridTemplateColumns: "repeat(auto-fit,minmax(220px,1fr))" } },
      fieldRow("Name", el("input", {
        class: "input", value: settings.assistant.name,
        onChange: (event) => patch("assistant.name", event.target.value),
      }), "Wird in der Oberfläche und als Anrede verwendet."),
      fieldRow("Sprache", selectFrom(
        [["de", "Deutsch"], ["en", "English"]], settings.assistant.language,
        (value) => patch("assistant.language", value))),
      fieldRow("Proaktivität", selectFrom(
        [["OFF", "Aus"], ["IMPORTANT_ONLY", "Nur Wichtiges"], ["NORMAL", "Normal"],
         ["PROACTIVE", "Proaktiv"]],
        settings.assistant.proactivity, (value) => patch("assistant.proactivity", value)),
        "Wie oft JARVIS von sich aus etwas meldet."),
      fieldRow("Coding-Modus", selectFrom(
        [["CHAT", "Nur Chat"], ["PLAN_ONLY", "Nur planen"], ["ASK_BEFORE_EDIT", "Vor Änderung fragen"],
         ["AUTO_EDIT", "Automatisch ändern"], ["FULL_AGENT", "Voller Agent"]],
        settings.assistant.coding_mode, (value) => patch("assistant.coding_mode", value))),
    ),
  );
}

function modelSection(settings) {
  return sectionCard("Modelle",
    el("div", { class: "col" },
      el("div", { class: "spread" },
        el("div", {},
          el("div", {}, "Nur kostenlose Modelle"),
          el("div", { class: "field__hint" },
            "Verhindert, dass ungefragt ein kostenpflichtiges Modell verwendet wird."),
        ),
        switchControl(settings.models.free_only, (value) => patch("models.free_only", value)),
      ),
      el("div", { class: "spread" },
        el("div", {},
          el("div", {}, "Offline-Modus"),
          el("div", { class: "field__hint" },
            "Deaktiviert Cloud-Anbieter, Web und externe Integrationen."),
        ),
        switchControl(settings.models.offline_mode, (value) => patch("models.offline_mode", value)),
      ),
      el("div", { class: "spread" },
        el("div", {},
          el("div", {}, "Zweite Meinung bei kritischen Aufgaben"),
          el("div", { class: "field__hint" },
            "Ein zweites Modell prüft wichtige technische Ergebnisse unabhängig."),
        ),
        switchControl(settings.models.multi_agent_review,
          (value) => patch("models.multi_agent_review", value)),
      ),
      el("div", { class: "divider" }),
      fieldRow("Router-Modus", selectFrom(
        [["AUTO", "Automatisch"], ["MANUAL", "Manuell"], ["FREE_ONLY", "Nur kostenlos"],
         ["LOCAL_ONLY", "Nur lokal"], ["HYBRID", "Hybrid (lokal bevorzugt)"],
         ["MULTI_MODEL", "Mehrere Modelle"]],
        settings.models.router_mode, (value) => patch("models.router_mode", value))),
      fieldRow("Free-Router", el("input", {
        class: "input mono", value: settings.models.free_router_model,
        onChange: (event) => patch("models.free_router_model", event.target.value),
      }), "Letzte Rückfallebene, wenn kein konkretes freies Modell passt."),
    ),
  );
}

function providerSection(settings, secrets, backend) {
  const secretMap = Object.fromEntries((secrets ?? []).map((s) => [s.name, s]));

  const secretField = (name) => {
    const info = secretMap[name] ?? { configured: false, preview: "" };
    const input = el("input", {
      class: "input mono",
      type: "password",
      placeholder: info.configured ? `gespeichert (${info.preview})` : "nicht hinterlegt",
      autocomplete: "off",
    });
    return el("div", { class: "field" },
      el("span", { class: "field__label" },
        SECRET_LABELS[name] ?? name,
        info.configured
          ? el("span", { class: "pill pill--ok tiny", style: { marginLeft: "8px" } }, "hinterlegt")
          : el("span", { class: "pill tiny", style: { marginLeft: "8px" } }, "fehlt"),
      ),
      el("div", { class: "row" },
        input,
        el("button", {
          class: "btn btn--sm btn--primary",
          onClick: async () => {
            if (!input.value.trim()) return;
            try {
              await api.setSecret(name, input.value.trim());
              input.value = "";
              toast("Gespeichert.", { kind: "ok" });
              renderSettings(document.getElementById("view"));
            } catch (error) {
              toast(error.message, { kind: "error" });
            }
          },
        }, "Speichern"),
        info.configured
          ? el("button", {
              class: "btn btn--sm btn--danger",
              onClick: async () => {
                await api.deleteSecret(name);
                toast("Entfernt.", { kind: "ok" });
                renderSettings(document.getElementById("view"));
              },
            }, "Löschen")
          : null,
      ),
    );
  };

  return sectionCard("Anbieter und Zugangsdaten",
    el("p", { class: "tiny muted", style: { marginTop: 0 } },
      `Speicherort: ${backend}. Werte werden nie zurückgegeben, nur ihr Status.`),
    el("div", { class: "col" },
      el("div", { class: "spread" },
        el("div", {}, "OpenRouter"),
        switchControl(settings.providers.openrouter_enabled,
          (value) => patch("providers.openrouter_enabled", value)),
      ),
      secretField("openrouter_api_key"),
      el("div", { class: "divider" }),
      el("div", { class: "spread" },
        el("div", {}, "Ollama (lokal)"),
        switchControl(settings.providers.ollama_enabled,
          (value) => patch("providers.ollama_enabled", value)),
      ),
      fieldRow("Ollama-Adresse", el("input", {
        class: "input mono", value: settings.providers.ollama_base_url,
        onChange: (event) => patch("providers.ollama_base_url", event.target.value),
      })),
      el("div", { class: "divider" }),
      el("div", { class: "spread" },
        el("div", {}, "Eigener OpenAI-kompatibler Server"),
        switchControl(settings.providers.custom_enabled,
          (value) => patch("providers.custom_enabled", value)),
      ),
      fieldRow("Basis-URL", el("input", {
        class: "input mono", value: settings.providers.custom_base_url,
        placeholder: "http://127.0.0.1:1234/v1",
        onChange: (event) => patch("providers.custom_base_url", event.target.value),
      })),
      el("div", { class: "spread" },
        el("div", {},
          el("div", { class: "small" }, "Als kostenlos behandeln"),
          el("div", { class: "field__hint" },
            "Für lokale Server ohne Preisangabe (LM Studio, llama.cpp). Sonst gilt er als kostenpflichtig."),
        ),
        switchControl(settings.providers.custom_is_free,
          (value) => patch("providers.custom_is_free", value)),
      ),
      secretField("github_token"),
      secretField("discord_bot_token"),
      secretField("nitrado_token"),
    ),
  );
}

function autonomySection(settings) {
  const level = String(settings.assistant.autonomy_level);
  const list = el("div", { class: "list" });
  for (const [value, title, description] of AUTONOMY) {
    list.append(el("button", {
      class: `list-item${value === level ? " list-item--active" : ""}`,
      style: { textAlign: "left", cursor: "pointer" },
      onClick: async () => {
        await api.setPermissions({ autonomy_level: Number(value) });
        toast(`Autonomie-Stufe ${value}: ${title}`, { kind: "ok" });
        renderSettings(document.getElementById("view"));
      },
    },
      el("span", { class: "pill tiny" }, value),
      el("div", { class: "grow" },
        el("div", {}, title),
        el("div", { class: "tiny muted" }, description),
      ),
      value === level ? icon("check", 16) : null,
    ));
  }
  return sectionCard("Autonomie-Stufe", list);
}

function permissionSection(settings) {
  const rows = Object.entries(settings.permissions ?? {});
  return sectionCard("Einzelne Berechtigungen",
    el("p", { class: "tiny muted", style: { marginTop: 0 } },
      "Diese Einstellungen gelten zusätzlich zur Autonomie-Stufe. Die strengere gewinnt."),
    el("div", { class: "list" }, ...rows.map(([capability, value]) =>
      el("div", { class: "list-item" },
        el("span", { class: "grow small" }, CAPABILITY_LABELS[capability] ?? capability),
        segmented(["DENY", "ASK", "ALLOW"], value, async (next) => {
          await api.setPermissions({ permissions: { [capability]: next } });
          renderSettings(document.getElementById("view"));
        }),
      ))),
  );
}

function sandboxSection(settings) {
  const folders = settings.security?.trusted_folders ?? [];
  const input = el("input", {
    class: "input mono",
    placeholder: "z. B. D:\\Projects",
  });

  const save = async (next) => {
    await api.setPermissions({ trusted_folders: next });
    toast("Freigegebene Ordner aktualisiert.", { kind: "ok" });
    renderSettings(document.getElementById("view"));
  };

  return sectionCard("Freigegebene Ordner",
    el("p", { class: "tiny muted", style: { marginTop: 0 } },
      "JARVIS darf nur innerhalb dieser Ordner lesen und schreiben. Ohne Eintrag ist jeder " +
      "Dateizugriff gesperrt."),
    el("div", { class: "list", style: { marginBottom: "10px" } }, ...folders.map((folder) =>
      el("div", { class: "list-item" },
        icon("folder", 15),
        el("span", { class: "grow mono small truncate", title: folder }, folder),
        el("button", {
          class: "btn btn--sm btn--danger",
          onClick: () => save(folders.filter((f) => f !== folder)),
        }, "Entfernen"),
      ))),
    el("div", { class: "row" },
      input,
      el("button", {
        class: "btn btn--primary",
        onClick: () => {
          const value = input.value.trim();
          if (value) save([...folders, value]);
        },
      }, icon("plus", 15), "Hinzufügen"),
    ),
  );
}

function appearanceSection(settings) {
  const accents = [
    ["ice", "Ice Blue"], ["amber", "Amber"], ["emerald", "Emerald"], ["violet", "Violet"],
  ];
  return sectionCard("Darstellung",
    el("div", { class: "col" },
      el("div", { class: "field" },
        el("span", { class: "field__label" }, "Akzentfarbe"),
        el("div", { class: "row wrap" }, ...accents.map(([value, label]) =>
          el("button", {
            class: `pill${settings.ui.accent === value ? " pill--accent" : ""}`,
            style: { cursor: "pointer" },
            onClick: async () => {
              document.documentElement.dataset.accent = value;
              await patch("ui.accent", value);
              renderSettings(document.getElementById("view"));
            },
          },
            el("span", {
              class: "dot",
              style: { color: { ice: "#4fd5ff", amber: "#ffb547", emerald: "#4ade80", violet: "#a78bfa" }[value] },
            }),
            label,
          ))),
      ),
      el("div", { class: "spread" },
        el("div", {},
          el("div", {}, "Bewegung reduzieren"),
          el("div", { class: "field__hint" }, "Schaltet Animationen ab."),
        ),
        switchControl(settings.ui.reduced_motion, (value) => patch("ui.reduced_motion", value)),
      ),
      el("div", { class: "spread" },
        el("div", {},
          el("div", {}, "Aktivitätsleiste anzeigen"),
          el("div", { class: "field__hint" }, "Rechte Spalte mit Verlauf und Werkzeugen."),
        ),
        switchControl(settings.ui.show_activity_panel,
          (value) => patch("ui.show_activity_panel", value)),
      ),
    ),
  );
}

function selectFrom(options, current, onChange) {
  return el("select", {
    class: "select",
    onChange: (event) => onChange(event.target.value),
  }, ...options.map(([value, label]) =>
    el("option", { value, selected: String(value) === String(current) }, label)));
}

export function applyAppearance(settings) {
  if (!settings) return;
  document.documentElement.dataset.accent = settings.ui?.accent ?? "ice";
  document.documentElement.dataset.reducedMotion = String(Boolean(settings.ui?.reduced_motion));
  document.documentElement.style.setProperty("--font-scale", String(settings.ui?.font_scale ?? 1));
  const brand = document.getElementById("brand-name");
  if (brand) brand.textContent = settings.assistant?.name ?? "JARVIS";
  const app = document.getElementById("app");
  if (app && window.innerWidth > 1080) {
    app.dataset.activity = settings.ui?.show_activity_panel === false ? "hidden" : "visible";
  }
}

export { state };
