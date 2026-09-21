/* Memory view (Spec §26): search, view, edit, pin, delete, export and import.

   The user has full control over what JARVIS remembers — including removing something it
   picked up automatically. */

import { api } from "../api.js";
import { clear, el, formatRelative, icon, mount } from "../dom.js";
import { confirmDialog, emptyState, openDialog, sectionCard, toast } from "../ui.js";

const KIND_LABELS = {
  preference: "Vorliebe", project: "Projekt", entity: "Person/Objekt",
  task: "Aufgabe", fact: "Fakt", workflow: "Arbeitsweise",
};

const KIND_CLASS = {
  preference: "pill--accent", project: "pill--violet", entity: "pill--ok",
  task: "pill--warn", fact: "", workflow: "pill--violet",
};

let filterKind = "";
let searchTerm = "";

export async function renderMemory(container) {
  const listHost = el("div", { class: "view__scroll", id: "memory-list" });

  mount(container,
    el("div", { class: "view__header" },
      el("div", {},
        el("h2", { class: "view__title" }, "Gedächtnis"),
        el("div", { class: "view__subtitle" },
          "Was JARVIS sich langfristig gemerkt hat. Zugangsdaten werden nie gespeichert."),
      ),
      el("div", { class: "row" },
        el("button", { class: "btn btn--sm", onClick: exportMemories },
          icon("copy", 14), "Exportieren"),
        el("button", { class: "btn btn--sm btn--primary", onClick: () => addDialog(listHost) },
          icon("plus", 14), "Eintrag anlegen"),
      ),
    ),
    el("div", { class: "row wrap", style: { padding: "0 var(--pad)" } },
      el("input", {
        class: "input", type: "search", style: { maxWidth: "280px" },
        placeholder: "Gedächtnis durchsuchen …",
        value: searchTerm,
        onInput: debounce((event) => { searchTerm = event.target.value; load(listHost); }, 280),
      }),
      el("div", { class: "row wrap grow" },
        ...["", ...Object.keys(KIND_LABELS)].map((kind) => el("button", {
          class: `pill${filterKind === kind ? " pill--accent" : ""}`,
          style: { cursor: "pointer" },
          onClick: (event) => {
            filterKind = kind;
            event.currentTarget.parentElement.querySelectorAll(".pill")
              .forEach((p) => p.classList.remove("pill--accent"));
            event.currentTarget.classList.add("pill--accent");
            load(listHost);
          },
        }, kind ? KIND_LABELS[kind] : "Alle")),
      ),
    ),
    listHost,
  );

  await load(listHost);
}

async function load(host) {
  mount(host, el("div", { class: "skeleton", style: { height: "90px" } }, "lädt"));
  try {
    const data = await api.memories({ query: searchTerm, kind: filterKind, limit: 200 });
    render(host, data);
  } catch (error) {
    mount(host, emptyState("warn", "Gedächtnis nicht verfügbar", error.message));
  }
}

function render(host, data) {
  clear(host);
  const memories = data.memories ?? [];
  const counts = data.counts ?? {};

  host.append(el("div", { class: "row wrap", style: { marginBottom: "var(--gap)" } },
    el("span", { class: "pill" }, `${counts.total ?? memories.length} Einträge`),
    el("span", { class: `pill ${data.mode === "AUTO" ? "pill--ok" : "pill--warn"}` },
      `Modus: ${data.mode}`),
    el("span", { class: `pill ${data.secret_filter ? "pill--ok" : "pill--danger"}` },
      data.secret_filter ? "Secret-Filter aktiv" : "Secret-Filter AUS"),
    ...Object.entries(counts)
      .filter(([key]) => key !== "total")
      .map(([kind, count]) => el("span", { class: "pill tiny" },
        `${KIND_LABELS[kind] ?? kind}: ${count}`)),
  ));

  if (!memories.length) {
    host.append(emptyState("memory", "Noch nichts gemerkt",
      searchTerm
        ? "Keine Treffer für diese Suche."
        : "JARVIS merkt sich automatisch, was langfristig relevant ist — Vorlieben, " +
          "Projektdetails, Arbeitsweisen. Du kannst auch selbst etwas eintragen."));
    return;
  }

  const list = el("div", { class: "list" });
  for (const memory of memories) list.append(memoryRow(memory, host));
  host.append(list);
}

function memoryRow(memory, host) {
  const row = el("div", {
    class: `list-item${memory.pinned ? " list-item--active" : ""}`,
    style: { alignItems: "flex-start", opacity: memory.disabled ? 0.5 : 1 },
  });

  row.append(el("div", { class: "grow", style: { minWidth: 0 } },
    el("div", { class: "row wrap", style: { gap: "6px", marginBottom: "3px" } },
      el("span", { class: `pill tiny ${KIND_CLASS[memory.kind] ?? ""}` },
        KIND_LABELS[memory.kind] ?? memory.kind),
      memory.subject ? el("strong", { class: "small" }, memory.subject) : null,
      memory.pinned ? el("span", { class: "pill tiny pill--accent" }, "angeheftet") : null,
      memory.disabled ? el("span", { class: "pill tiny" }, "deaktiviert") : null,
    ),
    el("div", { class: "small soft", style: { overflowWrap: "anywhere" } }, memory.content),
    el("div", { class: "tiny muted", style: { marginTop: "3px" } },
      `Wichtigkeit ${Math.round((memory.importance ?? 0) * 100)}% · ` +
      `${memory.hits ?? 0}× verwendet · ${formatRelative(memory.created_at)} · ${memory.source}`),
  ));

  row.append(el("div", { class: "row", style: { gap: "2px" } },
    iconButton(memory.pinned ? "check" : "pin", memory.pinned ? "Lösen" : "Anheften",
      async () => {
        await api.updateMemory(memory.id, { pinned: !memory.pinned });
        load(host);
      }),
    iconButton("edit", "Bearbeiten", () => editDialog(memory, host)),
    iconButton("trash", "Löschen", async () => {
      const confirmed = await confirmDialog(
        "Eintrag löschen?", `„${memory.subject || memory.content.slice(0, 70)}“ wird entfernt.`,
        { confirmLabel: "Löschen", variant: "risk" },
      );
      if (!confirmed) return;
      await api.deleteMemory(memory.id);
      toast("Eintrag gelöscht.", { kind: "ok" });
      load(host);
    }),
  ));
  return row;
}

function iconButton(name, title, onClick) {
  return el("button", { class: "btn btn--icon btn--ghost btn--sm", title, "aria-label": title, onClick },
    icon(name, 14));
}

function memoryForm(memory = {}) {
  const subject = el("input", { class: "input", value: memory.subject ?? "",
                                placeholder: "Kurzer Titel" });
  const content = el("textarea", { class: "textarea", placeholder: "Eine präzise Aussage" },
                     memory.content ?? "");
  const kind = el("select", { class: "select" },
    ...Object.entries(KIND_LABELS).map(([value, label]) =>
      el("option", { value, selected: value === (memory.kind ?? "fact") }, label)));
  const importance = el("input", {
    class: "input", type: "number", min: "0", max: "1", step: "0.05",
    value: String(memory.importance ?? 0.7),
  });

  const body = el("div", { class: "col" },
    el("label", { class: "field" }, el("span", { class: "field__label" }, "Titel"), subject),
    el("label", { class: "field" }, el("span", { class: "field__label" }, "Inhalt"), content),
    el("div", { class: "row" },
      el("label", { class: "field grow" }, el("span", { class: "field__label" }, "Art"), kind),
      el("label", { class: "field grow" },
        el("span", { class: "field__label" }, "Wichtigkeit"), importance),
    ),
    el("p", { class: "tiny muted", style: { margin: 0 } },
      "Passwörter, Schlüssel und Tokens werden abgelehnt — sie gehören in die Einstellungen."),
  );
  return { body, read: () => ({
    subject: subject.value.trim(),
    content: content.value.trim(),
    kind: kind.value,
    importance: Number(importance.value),
  }) };
}

async function addDialog(host) {
  const form = memoryForm();
  const answer = await openDialog({
    title: "Neuer Gedächtniseintrag",
    body: form.body,
    actions: [
      { label: "Abbrechen", value: null, class: "btn--ghost" },
      { label: "Speichern", value: "save", class: "btn--primary" },
    ],
  }).promise;
  if (answer !== "save") return;
  try {
    await api.createMemory(form.read());
    toast("Gespeichert.", { kind: "ok" });
    load(host);
  } catch (error) {
    toast(error.message, { kind: "error", timeout: 9000 });
  }
}

async function editDialog(memory, host) {
  const form = memoryForm(memory);
  const answer = await openDialog({
    title: "Eintrag bearbeiten",
    body: form.body,
    actions: [
      { label: "Abbrechen", value: null, class: "btn--ghost" },
      { label: "Speichern", value: "save", class: "btn--primary" },
    ],
  }).promise;
  if (answer !== "save") return;
  try {
    await api.updateMemory(memory.id, form.read());
    toast("Aktualisiert.", { kind: "ok" });
    load(host);
  } catch (error) {
    toast(error.message, { kind: "error", timeout: 9000 });
  }
}

async function exportMemories() {
  try {
    const data = await api.exportMemories();
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = el("a", { href: url, download: `jarvis-gedaechtnis-${Date.now()}.json` });
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    toast(`${data.count} Einträge exportiert.`, { kind: "ok" });
  } catch (error) {
    toast(error.message, { kind: "error" });
  }
}

function debounce(fn, delay) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), delay); };
}

export { sectionCard };
