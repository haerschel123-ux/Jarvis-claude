/* Tool inspector (Spec §67, §89): what exists, what it may do, and what it did. */

import { api } from "../api.js";
import { el, formatDateTime, icon, mount } from "../dom.js";
import { emptyState, sectionCard, toast } from "../ui.js";

const RISK_CLASS = {
  SAFE_READ: "pill--ok",
  SAFE_ACTION: "pill--ok",
  WRITE: "pill--warn",
  EXTERNAL_WRITE: "pill--warn",
  SYSTEM_CONTROL: "pill--warn",
  DESTRUCTIVE: "pill--danger",
  PRIVILEGED: "pill--danger",
};

const PERMISSION_CLASS = { ALLOW: "pill--ok", ASK: "pill--warn", DENY: "pill--danger" };

export async function renderTools(container) {
  const scroll = el("div", { class: "view__scroll" });
  mount(container,
    el("div", { class: "view__header" },
      el("div", {},
        el("h2", { class: "view__title" }, "Werkzeuge"),
        el("div", { class: "view__subtitle" },
          "Jeder Aufruf läuft durch dieselbe Prüfung — auch der aus dieser Oberfläche."),
      ),
    ),
    scroll,
  );

  scroll.append(el("div", { class: "skeleton", style: { height: "120px" } }, "lädt"));

  const [toolsResult, runsResult, grantsResult] = await Promise.allSettled([
    api.tools(), api.toolRuns(40), api.grants(),
  ]);

  mount(scroll,
    grantsResult.status === "fulfilled" ? grantsCard(grantsResult.value.grants ?? []) : null,
    toolsResult.status === "fulfilled"
      ? toolsSection(toolsResult.value)
      : emptyState("warn", "Werkzeuge nicht verfügbar", toolsResult.reason?.message ?? ""),
    runsResult.status === "fulfilled" ? runsSection(runsResult.value.runs ?? []) : null,
  );
}

function toolsSection(data) {
  const tools = data.tools ?? [];
  const sandbox = data.sandbox ?? {};
  const byTag = new Map();
  for (const tool of tools) {
    const tag = tool.tags?.[0] ?? "sonstige";
    if (!byTag.has(tag)) byTag.set(tag, []);
    byTag.get(tag).push(tool);
  }

  const section = el("div", { class: "col", style: { gap: "var(--gap)" } });

  section.append(sectionCard("Dateisystem-Sandbox",
    sandbox.configured
      ? el("div", { class: "col", style: { gap: "6px" } },
          ...(sandbox.trusted_folders ?? []).map((folder) =>
            el("div", { class: "list-item mono small" }, icon("folder", 14), folder)))
      : el("div", { class: "pill pill--warn" },
          "Kein Ordner freigegeben — Dateizugriffe werden abgelehnt."),
    el("p", { class: "tiny muted", style: { marginBottom: 0 } },
      `Gesperrt bleiben immer: ${(sandbox.protected_filenames ?? []).join(", ")} sowie geschützte Systembereiche.`),
  ));

  for (const [tag, group] of byTag) {
    const list = el("div", { class: "list" });
    for (const tool of group) {
      list.append(el("div", { class: "list-item", style: { alignItems: "flex-start" } },
        el("div", { class: "grow", style: { minWidth: 0 } },
          el("div", { class: "row", style: { gap: "8px" } },
            el("strong", { class: "mono small" }, tool.name),
            el("span", { class: `pill tiny ${RISK_CLASS[tool.risk_level] ?? ""}` }, tool.risk_level),
            el("span", { class: `pill tiny ${PERMISSION_CLASS[tool.permission] ?? ""}` },
              tool.permission),
            !tool.available ? el("span", { class: "pill tiny pill--warn" }, "nicht verfügbar") : null,
          ),
          el("div", { class: "tiny muted", style: { marginTop: "3px" } }, tool.description),
          !tool.available && tool.unavailable_reason
            ? el("div", { class: "tiny", style: { color: "var(--warn)" } }, tool.unavailable_reason)
            : null,
        ),
      ));
    }
    section.append(sectionCard(tag, list));
  }
  return section;
}

function grantsCard(grants) {
  if (!grants.length) return null;
  return sectionCard("Dauerhafte Freigaben",
    el("div", { class: "list" }, ...grants.map((grant) =>
      el("div", { class: "list-item" },
        el("span", { class: "mono small grow truncate" }, `${grant.capability} · ${grant.scope}`),
        el("button", {
          class: "btn btn--sm btn--danger",
          onClick: async (event) => {
            await api.revokeGrant(grant.capability, grant.scope);
            toast("Freigabe widerrufen.", { kind: "ok" });
            event.currentTarget.closest(".list-item")?.remove();
          },
        }, "Widerrufen"),
      ))),
  );
}

function runsSection(runs) {
  if (!runs.length) {
    return sectionCard("Letzte Ausführungen",
      emptyState("clock", "Noch nichts ausgeführt",
        "Hier erscheint jeder Werkzeugaufruf mit Ergebnis und Dauer."));
  }
  return sectionCard("Letzte Ausführungen",
    el("div", { class: "list" }, ...runs.map((run) =>
      el("div", { class: "list-item", style: { alignItems: "flex-start" } },
        el("span", {
          class: "dot",
          style: {
            marginTop: "6px",
            color: run.status === "success" ? "var(--ok)"
              : run.status === "denied" ? "var(--warn)" : "var(--danger)",
          },
        }),
        el("div", { class: "grow", style: { minWidth: 0 } },
          el("div", { class: "row", style: { gap: "8px" } },
            el("strong", { class: "mono small" }, run.tool),
            el("span", { class: `pill tiny ${RISK_CLASS[run.risk_level] ?? ""}` }, run.risk_level),
            el("span", { class: "tiny muted" }, `${run.duration_ms ?? "—"} ms`),
          ),
          el("div", { class: "tiny muted truncate", title: run.result_summary || run.error || "" },
            run.error || run.result_summary || "—"),
          el("div", { class: "tiny muted" }, formatDateTime(run.created_at)),
        ),
      ))),
  );
}
