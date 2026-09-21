/* Model browser (Spec §8): the filters and columns the specification asks for, with missing
   provider metadata shown as "Unknown" rather than guessed. */

import { api } from "../api.js";
import { clear, el, formatNumber, formatPrice, icon, mount } from "../dom.js";
import { state } from "../state.js";
import { emptyState, toast } from "../ui.js";

const FILTERS = [
  { key: "free", label: "Free" },
  { key: "tools", label: "Tools" },
  { key: "vision", label: "Vision" },
  { key: "structured", label: "Structured" },
  { key: "reasoning", label: "Reasoning" },
  { key: "local", label: "Lokal" },
];

const active = new Set();
let search = "";

export async function renderModels(container) {
  const listHost = el("div", { class: "view__scroll", id: "model-list" });

  const filterBar = el("div", { class: "row wrap", style: { padding: "0 var(--pad)" } },
    el("div", { class: "row wrap grow" },
      ...FILTERS.map((filter) => el("button", {
        class: `pill${active.has(filter.key) ? " pill--accent" : ""}`,
        style: { cursor: "pointer" },
        dataset: { filter: filter.key },
        onClick: (event) => {
          if (active.has(filter.key)) active.delete(filter.key);
          else active.add(filter.key);
          event.currentTarget.classList.toggle("pill--accent", active.has(filter.key));
          load(listHost);
        },
      }, filter.label)),
    ),
    el("input", {
      class: "input",
      style: { maxWidth: "240px" },
      type: "search",
      placeholder: "Modell suchen …",
      onInput: debounce((event) => { search = event.target.value; load(listHost); }, 280),
    }),
    el("button", {
      class: "btn btn--sm",
      onClick: async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
          const result = await api.refreshModels();
          toast(`${result.models} Modelle geladen.`, { kind: "ok" });
          const failed = Object.entries(result.errors ?? {});
          for (const [provider, message] of failed) {
            toast(`${provider}: ${message}`, { kind: "warn", timeout: 8000 });
          }
        } catch (error) {
          toast(error.message, { kind: "error" });
        } finally {
          button.disabled = false;
          load(listHost);
        }
      },
    }, icon("refresh", 14), "Katalog aktualisieren"),
  );

  mount(container,
    el("div", { class: "view__header" },
      el("div", {},
        el("h2", { class: "view__title" }, "Modelle"),
        el("div", { class: "view__subtitle" },
          "Dynamisch von den Anbietern geladen. Fehlende Angaben stehen als „Unknown“."),
      ),
    ),
    filterBar,
    listHost,
  );

  await load(listHost);
}

async function load(host) {
  mount(host, el("div", { class: "skeleton", style: { height: "120px" } }, "lädt"));
  try {
    const data = await api.models({
      free: active.has("free") ? true : undefined,
      local: active.has("local") ? true : undefined,
      tools: active.has("tools"),
      vision: active.has("vision"),
      structured: active.has("structured"),
      reasoning: active.has("reasoning"),
      search: search || undefined,
    });
    renderList(host, data);
  } catch (error) {
    mount(host, emptyState("warn", "Modelle konnten nicht geladen werden", error.message));
  }
}

function renderList(host, data) {
  clear(host);
  const models = data.models ?? [];

  host.append(el("div", { class: "row wrap small muted", style: { marginBottom: "12px" } },
    el("span", {}, `${models.length} von ${data.total} Modellen`),
    data.free_only ? el("span", { class: "pill pill--ok tiny" }, "FREE ONLY ist aktiv") : null,
    data.stale ? el("span", { class: "pill pill--warn tiny" }, "Katalog ist veraltet") : null,
  ));

  if (!models.length) {
    host.append(emptyState("search", "Keine Modelle gefunden",
      data.total === 0
        ? "Hinterlege einen OpenRouter-Schlüssel oder starte Ollama und aktualisiere den Katalog."
        : "Keine Treffer für diese Filter."));
    return;
  }

  const grid = el("div", { class: "grid grid--wide" });
  for (const model of models) grid.append(modelCard(model));
  host.append(grid);
}

function modelCard(model) {
  const display = model.display ?? {};
  const flag = (value, label) => {
    const known = value === "Yes" || value === "No";
    const cls = value === "Yes" ? "pill--ok" : value === "No" ? "" : "pill--warn";
    return el("span", { class: `pill tiny ${cls}`, title: known ? "" : "Der Anbieter macht dazu keine Angabe" },
      `${label}: ${value}`);
  };

  return el("article", { class: `card card--hover${model.is_free ? " card--lit" : ""}` },
    el("div", { class: "spread", style: { alignItems: "flex-start", marginBottom: "8px" } },
      el("div", { style: { minWidth: 0 } },
        el("strong", { class: "truncate", style: { display: "block" }, title: model.name },
          model.name),
        el("div", { class: "tiny mono muted truncate", title: model.id }, model.id),
      ),
      el("div", { class: "row", style: { gap: "4px" } },
        model.is_free ? el("span", { class: "pill pill--ok tiny" }, "free") : null,
        model.is_local ? el("span", { class: "pill pill--violet tiny" }, "lokal") : null,
        model.is_router ? el("span", { class: "pill pill--accent tiny" }, "Router") : null,
      ),
    ),
    model.description
      ? el("p", { class: "tiny muted", style: { margin: "0 0 10px", maxHeight: "3.2em", overflow: "hidden" } },
          model.description)
      : null,
    el("div", { class: "row wrap", style: { gap: "4px", marginBottom: "10px" } },
      flag(display.tools, "Tools"),
      flag(display.vision, "Vision"),
      flag(display.structured_output, "Struktur"),
      flag(display.reasoning, "Reasoning"),
    ),
    el("dl", { class: "kv tiny" },
      el("dt", {}, "Anbieter"), el("dd", {}, model.provider),
      el("dt", {}, "Kontext"), el("dd", {},
        typeof display.context === "number" ? formatNumber(display.context) : String(display.context)),
      el("dt", {}, "Preis rein"), el("dd", {}, formatPrice(model.price_prompt)),
      el("dt", {}, "Preis raus"), el("dd", {}, formatPrice(model.price_completion)),
    ),
  );
}

function debounce(fn, delay) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}

export { state };
