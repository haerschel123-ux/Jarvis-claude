/* Automations view (Spec §22, §27, §28, §88): reminders, proactivity and backups. */

import { api } from "../api.js";
import { clear, el, formatDateTime, formatRelative, icon, mount } from "../dom.js";
import { confirmDialog, emptyState, sectionCard, toast } from "../ui.js";

const KIND_LABELS = {
  one_time: "einmalig", daily: "täglich", weekly: "wöchentlich",
  monthly: "monatlich", interval: "Intervall",
};

const TIMEZONE = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";

export async function renderAutomations(container) {
  const host = el("div", { class: "view__scroll", id: "automations" });
  mount(container,
    el("div", { class: "view__header" },
      el("div", {},
        el("h2", { class: "view__title" }, "Automationen"),
        el("div", { class: "view__subtitle" },
          "Erinnerungen laufen lokal — sie brauchen weder Internet noch ein KI-Modell."),
      ),
      el("button", { class: "btn btn--sm", onClick: () => load(host) },
        icon("refresh", 14), "Aktualisieren"),
    ),
    host,
  );
  await load(host);
}

async function load(host) {
  mount(host, el("div", { class: "skeleton", style: { height: "90px" } }, "lädt"));
  const [reminders, backups, proactive] = await Promise.allSettled([
    api.reminders(true), api.backups(), api.proactiveState(),
  ]);
  clear(host);
  host.append(createReminderCard(host));
  host.append(el("div", { class: "grid grid--wide", style: { marginTop: "var(--gap)" } },
    remindersCard(reminders, host),
    proactiveCard(proactive),
    backupsCard(backups, host),
  ));
}

function createReminderCard(host) {
  const input = el("input", {
    class: "input",
    placeholder: "z. B. „morgen um 18 Uhr den DayZ Server prüfen“",
    onInput: debounce(async (event) => {
      const preview = document.getElementById("reminder-preview");
      const text = event.target.value.trim();
      if (!preview) return;
      if (text.length < 4) {
        preview.textContent = "";
        return;
      }
      try {
        const { parsed } = await api.previewReminder(text, TIMEZONE);
        preview.textContent = parsed
          ? `Erkannt: ${formatDateTime(parsed.when)} (${KIND_LABELS[parsed.kind] ?? parsed.kind}) — „${parsed.text}“`
          : "Kein Zeitpunkt erkannt. Nenne eine Uhrzeit, einen Tag oder einen Abstand.";
        preview.style.color = parsed ? "var(--ok)" : "var(--warn)";
      } catch {
        preview.textContent = "";
      }
    }, 320),
    onKeydown: (event) => { if (event.key === "Enter") submit(); },
  });

  async function submit() {
    const text = input.value.trim();
    if (!text) return;
    try {
      const result = await api.createReminder({ text, timezone: TIMEZONE });
      toast(
        result.parsed
          ? `Erinnerung für ${formatDateTime(result.parsed.when)} gespeichert.`
          : "Erinnerung gespeichert.",
        { kind: "ok" },
      );
      input.value = "";
      document.getElementById("reminder-preview").textContent = "";
      load(host);
    } catch (error) {
      toast(error.message, { kind: "error", timeout: 9000 });
    }
  }

  return sectionCard("Neue Erinnerung",
    el("div", { class: "row" }, input,
      el("button", { class: "btn btn--primary", onClick: submit }, icon("plus", 15), "Anlegen")),
    el("div", { id: "reminder-preview", class: "tiny", style: { marginTop: "8px", minHeight: "1.3em" } }),
    el("div", { class: "tiny muted" }, `Zeitzone: ${TIMEZONE}`),
  );
}

function remindersCard(result, host) {
  if (result.status !== "fulfilled") {
    return sectionCard("Erinnerungen", el("div", { class: "muted small" }, "Nicht verfügbar"));
  }
  const reminders = result.value.reminders ?? [];
  if (!reminders.length) {
    return sectionCard("Erinnerungen",
      emptyState("clock", "Keine Erinnerungen",
        "Erinnerungen überstehen einen Neustart. Verpasste werden beim nächsten Start ausgelöst."));
  }
  return sectionCard(`Erinnerungen (${reminders.length})`,
    el("div", { class: "list" }, ...reminders.map((reminder) =>
      el("div", { class: "list-item", style: { alignItems: "flex-start",
                                               opacity: reminder.enabled ? 1 : 0.5 } },
        icon("clock", 15),
        el("div", { class: "grow", style: { minWidth: 0 } },
          el("div", { class: "small" }, reminder.text),
          el("div", { class: "tiny muted" },
            `${formatDateTime(reminder.next_run_at)} · ${KIND_LABELS[reminder.schedule_kind] ?? reminder.schedule_kind}` +
            (reminder.fire_count ? ` · ${reminder.fire_count}× ausgelöst` : "") +
            (reminder.missed_count ? ` · ${reminder.missed_count}× verpasst` : "")),
        ),
        el("div", { class: "row", style: { gap: "2px" } },
          el("button", {
            class: "btn btn--icon btn--ghost btn--sm",
            title: reminder.enabled ? "Pausieren" : "Aktivieren",
            onClick: async () => {
              await api.setReminderEnabled(reminder.id, !reminder.enabled);
              load(host);
            },
          }, icon(reminder.enabled ? "stop" : "check", 14)),
          el("button", {
            class: "btn btn--icon btn--ghost btn--sm",
            title: "Löschen",
            onClick: async () => {
              const ok = await confirmDialog("Erinnerung löschen?", reminder.text,
                                             { confirmLabel: "Löschen", variant: "risk" });
              if (!ok) return;
              await api.deleteReminder(reminder.id);
              toast("Gelöscht.", { kind: "ok" });
              load(host);
            },
          }, icon("trash", 14)),
        ),
      ))),
  );
}

function proactiveCard(result) {
  if (result.status !== "fulfilled") return null;
  const stats = result.value;
  return sectionCard("Proaktive Meldungen",
    el("p", { class: "tiny muted", style: { marginTop: 0 } },
      "JARVIS meldet sich von sich aus nur bei relevanten Dingen. Identische Meldungen " +
      "werden zusammengefasst, und es gibt eine Obergrenze pro Zeitfenster."),
    el("dl", { class: "kv small" },
      el("dt", {}, "Quellen"),
      el("dd", {}, (stats.watchers ?? []).join(", ") || "noch keine registriert"),
      el("dt", {}, "Letzte 5 Min."), el("dd", {}, `${stats.recent_window} von ${stats.rate_limit}`),
      el("dt", {}, "Unterdrückt"), el("dd", {}, String(stats.suppressed_total ?? 0)),
    ),
  );
}

function backupsCard(result, host) {
  if (result.status !== "fulfilled") return null;
  const backups = result.value.backups ?? [];
  return sectionCard("Backups",
    el("div", { class: "spread", style: { marginBottom: "10px" } },
      el("span", { class: `pill tiny ${result.value.due ? "pill--warn" : "pill--ok"}` },
        result.value.due ? "fällig" : "aktuell"),
      el("button", {
        class: "btn btn--sm",
        onClick: async (event) => {
          event.currentTarget.disabled = true;
          try {
            await api.createBackup("manuell");
            toast("Backup erstellt.", { kind: "ok" });
            load(host);
          } catch (error) {
            toast(error.message, { kind: "error" });
          }
        },
      }, "Jetzt sichern"),
    ),
    backups.length
      ? el("div", { class: "list" }, ...backups.slice(0, 8).map((backup) =>
          el("div", { class: "row small" },
            icon("shield", 14),
            el("span", { class: "grow truncate mono tiny" }, backup.name),
            el("span", { class: "tiny muted" }, formatRelative(backup.created_at)),
          )))
      : el("div", { class: "small muted" }, "Noch kein Backup vorhanden."),
    el("p", { class: "tiny muted", style: { marginBottom: 0 } },
      "Enthält Datenbank, Einstellungen und Gedächtnis. Zugangsdaten bleiben ausgeschlossen."),
  );
}

function debounce(fn, delay) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), delay); };
}
