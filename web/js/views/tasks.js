/* Task view (Spec §102, §103): what JARVIS is working on, with progress and a stop button. */

import { api } from "../api.js";
import { clear, el, formatRelative, icon, mount } from "../dom.js";
import { emptyState, toast } from "../ui.js";

const STATUS = {
  PENDING: ["Wartet", ""],
  RUNNING: ["Läuft", "pill--accent"],
  WAITING: ["Pausiert", "pill--warn"],
  WAITING_PERMISSION: ["Wartet auf Bestätigung", "pill--warn"],
  DONE: ["Fertig", "pill--ok"],
  FAILED: ["Fehlgeschlagen", "pill--danger"],
  CANCELLED: ["Abgebrochen", ""],
};

export async function renderTasks(container) {
  const host = el("div", { class: "view__scroll", id: "task-list" });
  mount(container,
    el("div", { class: "view__header" },
      el("div", {},
        el("h2", { class: "view__title" }, "Aufgaben"),
        el("div", { class: "view__subtitle" },
          "Mehrstufige Aufträge mit Fortschritt. Jede laufende Aufgabe lässt sich abbrechen."),
      ),
      el("button", { class: "btn btn--sm", onClick: () => load(host) },
        icon("refresh", 14), "Aktualisieren"),
    ),
    host,
  );
  await load(host);
}

async function load(host) {
  mount(host, el("div", { class: "skeleton", style: { height: "80px" } }, "lädt"));
  try {
    const data = await api.tasks();
    render(host, data);
  } catch (error) {
    mount(host, emptyState("warn", "Aufgaben nicht verfügbar", error.message));
  }
}

function render(host, data) {
  clear(host);
  const tasks = data.tasks ?? [];
  if (!tasks.length) {
    host.append(emptyState("tasks", "Keine Aufgaben",
      "Sobald JARVIS etwas Mehrstufiges übernimmt, siehst du hier den Plan, den Fortschritt " +
      "und einen Abbruch-Knopf."));
    return;
  }

  const running = new Set(data.running ?? []);
  const list = el("div", { class: "col" });
  for (const task of tasks) list.append(taskCard(task, running.has(task.id), host));
  host.append(list);
}

function taskCard(task, isRunning, host) {
  const [label, pillClass] = STATUS[task.status] ?? ["Unbekannt", ""];
  const progress = Math.round((task.progress ?? 0) * 100);
  const active = !["DONE", "FAILED", "CANCELLED"].includes(task.status);

  return el("article", { class: "card card--hover" },
    el("div", { class: "spread", style: { alignItems: "flex-start" } },
      el("div", { style: { minWidth: 0 } },
        el("strong", {}, task.title),
        task.goal ? el("div", { class: "small muted" }, task.goal) : null,
      ),
      el("div", { class: "row" },
        el("span", { class: `pill tiny ${pillClass}` },
          isRunning ? el("span", { class: "dot dot--live" }) : null, label),
        active
          ? el("button", {
              class: "btn btn--sm btn--danger",
              onClick: async () => {
                await api.cancelTask(task.id);
                toast("Aufgabe abgebrochen.", { kind: "info" });
                load(host);
              },
            }, "Abbrechen")
          : null,
      ),
    ),
    el("div", { class: "meter", style: { marginTop: "12px" } },
      el("div", {
        class: `meter__fill${task.status === "FAILED" ? " meter__fill--danger" : ""}`,
        style: { width: `${progress}%` },
      })),
    el("div", { class: "row spread tiny muted", style: { marginTop: "6px" } },
      el("span", {}, `${progress}%`),
      el("span", {}, formatRelative(task.created_at)),
    ),
    task.error
      ? el("div", { class: "small", style: { color: "var(--danger)", marginTop: "8px" } }, task.error)
      : null,
    el("button", {
      class: "btn btn--sm btn--ghost",
      style: { marginTop: "8px" },
      onClick: (event) => expand(task.id, event.currentTarget),
    }, "Schritte anzeigen"),
  );
}

async function expand(taskId, button) {
  const existing = button.parentElement.querySelector("[data-steps]");
  if (existing) {
    existing.remove();
    button.textContent = "Schritte anzeigen";
    return;
  }
  button.textContent = "Schritte verbergen";
  try {
    const { task } = await api.task(taskId);
    const steps = task.steps ?? [];
    const box = el("div", { dataset: { steps: "1" }, class: "list", style: { marginTop: "8px" } },
      ...(steps.length
        ? steps.map((step) => el("div", { class: "row small" },
            el("span", {
              class: "dot",
              style: {
                color: step.status === "DONE" ? "var(--ok)"
                  : step.status === "FAILED" ? "var(--danger)"
                  : step.status === "RUNNING" ? "var(--accent)" : "var(--text-mute)",
              },
            }),
            el("span", { class: "grow" }, step.title),
            el("span", { class: "tiny muted" }, STATUS[step.status]?.[0] ?? step.status),
          ))
        : [el("div", { class: "small muted" }, "Keine Schritte hinterlegt.")]),
    );
    button.after(box);
  } catch (error) {
    toast(error.message, { kind: "error" });
  }
}
