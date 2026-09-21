/* Views whose backend arrives in a later stage.

   These deliberately state what is already built and what is still missing, instead of
   showing a fake, non-functional interface (Spec §108). Each one links to the part of the
   system that already works. */

import { el, icon, mount } from "../dom.js";
import { navigate } from "../router.js";

const PLANNED = {
  projects: {
    title: "Projekte",
    subtitle: "Arbeitsbereiche mit eigenem Gedächtnis, Repository und Anweisungen",
    ready: [
      "Datenbankschema für Projekte, Projektdateien und projektbezogene Unterhaltungen",
      "Zuordnung von Unterhaltungen und Aufgaben zu einem Projekt",
    ],
    pending: [
      "Projektverwaltung in der Oberfläche",
      "Projektbezogene Anweisungen im Kontext",
      "Automatische Erkennung von Repository und Branch",
    ],
    hint: "Bis dahin kannst du Arbeitsordner unter Einstellungen → Freigegebene Ordner hinterlegen.",
    goto: { label: "Zu den Einstellungen", route: "settings" },
  },
  integrations: {
    title: "Integrationen",
    subtitle: "GitHub, Discord, DayZ/Nitrado, Google und Home Assistant",
    ready: [
      "Lokale Git-Werkzeuge: Status, Diff, Log, Commit, Branch und Push",
      "Sichere Ablage der Zugangsdaten mit getrennter Berechtigung je Dienst",
    ],
    pending: [
      "GitHub-API: Repositories, Issues, Pull Requests",
      "Discord-Bot-Projekte überwachen und reparieren",
      "DayZ/Nitrado: XML-Validierung, RPT-Analyse, Serverstatus",
    ],
    hint: "Zugangsdaten kannst du schon jetzt hinterlegen — sie werden sicher gespeichert.",
    goto: { label: "Zugangsdaten hinterlegen", route: "settings" },
  },
};

export function renderPlaceholder(container, key) {
  const info = PLANNED[key];
  if (!info) return;

  const scroll = el("div", { class: "view__scroll" });
  mount(container,
    el("div", { class: "view__header" },
      el("div", {},
        el("h2", { class: "view__title" }, info.title),
        el("div", { class: "view__subtitle" }, info.subtitle),
      ),
      el("span", { class: "pill pill--warn" }, "in Arbeit"),
    ),
    scroll,
  );

  scroll.append(el("div", { class: "grid grid--wide" },
    el("section", { class: "card card--lit" },
      el("h3", { class: "card__title" }, icon("check", 14), "Bereits vorhanden"),
      el("ul", { style: { margin: 0, paddingLeft: "1.1em" } },
        ...info.ready.map((item) => el("li", { class: "small soft" }, item))),
    ),
    el("section", { class: "card" },
      el("h3", { class: "card__title" }, icon("clock", 14), "Noch offen"),
      el("ul", { style: { margin: 0, paddingLeft: "1.1em" } },
        ...info.pending.map((item) => el("li", { class: "small muted" }, item))),
    ),
  ));

  if (info.hint || info.goto) {
    scroll.append(el("div", { class: "card", style: { marginTop: "var(--gap)" } },
      el("div", { class: "row wrap" },
        icon("info", 16),
        el("span", { class: "small soft grow" }, info.hint),
        info.goto
          ? el("button", {
              class: "btn btn--sm",
              onClick: () => navigate(info.goto.route),
            }, info.goto.label)
          : null,
      ),
    ));
  }
}
