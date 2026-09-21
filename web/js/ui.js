/* Shared UI pieces: the voice orb, toasts, dialogs and the permission prompt. */

import { clear, el, escapeHtml, icon } from "./dom.js";

/* --- voice orb (Spec §56) ------------------------------------------------------------- */

export const ORB_STATES = {
  IDLE: { label: "Bereit", detail: "" },
  LISTENING: { label: "Hört zu", detail: "Sprich einfach los." },
  RECOGNIZING: { label: "Erkennt", detail: "Ich werte die Aufnahme aus." },
  THINKING: { label: "Denkt nach", detail: "" },
  ACTING: { label: "Führt aus", detail: "" },
  SPEAKING: { label: "Spricht", detail: "" },
  ERROR: { label: "Fehler", detail: "" },
  PAUSED: { label: "Pausiert", detail: "" },
};

const WAVE_BARS = 9;

export function createOrb({ size = "", withLabel = false } = {}) {
  const orb = el("div", {
    class: `orb ${size ? `orb--${size}` : ""}`.trim(),
    dataset: { state: "IDLE" },
    role: "img",
    "aria-label": "Zustand: Bereit",
  });

  orb.append(el("span", { class: "orb__halo" }));
  orb.append(el("span", { class: "orb__pulse" }));

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "orb__svg");
  svg.setAttribute("viewBox", "0 0 100 100");
  svg.setAttribute("aria-hidden", "true");
  for (const [cls, radius] of [["outer", 48], ["mid", 40], ["inner", 32]]) {
    const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    circle.setAttribute("class", `orb__ring orb__ring--${cls}`);
    circle.setAttribute("cx", "50");
    circle.setAttribute("cy", "50");
    circle.setAttribute("r", String(radius));
    svg.append(circle);
  }
  orb.append(svg);

  const wave = el("span", { class: "orb__wave" });
  for (let index = 0; index < WAVE_BARS; index += 1) wave.append(el("span"));
  orb.append(wave);

  orb.append(el("span", { class: "orb__core" }));
  if (withLabel) orb.append(el("span", { class: "orb__label" }, ORB_STATES.IDLE.label));
  return orb;
}

export function setOrbState(orb, stateName) {
  if (!orb) return;
  const info = ORB_STATES[stateName] ?? ORB_STATES.IDLE;
  orb.dataset.state = stateName;
  orb.setAttribute("aria-label", `Zustand: ${info.label}`);
  const label = orb.querySelector(".orb__label");
  if (label) label.textContent = info.label;
}

/**
 * Drive the orb's waveform from a 0..1 level.
 * Without a real microphone level a gentle synthetic motion is used, so the orb still
 * communicates "active" without pretending to show real audio.
 */
export function setOrbLevel(orb, level = null) {
  if (!orb) return;
  const bars = orb.querySelectorAll(".orb__wave span");
  const time = performance.now() / 260;
  bars.forEach((bar, index) => {
    const base = level === null
      ? 0.35 + 0.3 * Math.abs(Math.sin(time + index * 0.7))
      : Math.min(1, level * (0.55 + 0.65 * Math.abs(Math.sin(time * 1.6 + index * 0.9))));
    const distance = Math.abs(index - (WAVE_BARS - 1) / 2) / ((WAVE_BARS - 1) / 2);
    const height = 12 + base * 58 * (1 - distance * 0.55);
    bar.style.height = `${height}%`;
  });
}

/* --- toasts --------------------------------------------------------------------------- */

const TOAST_ICONS = { ok: "check", warn: "warn", error: "warn", info: "info" };

export function toast(message, { kind = "info", timeout = 5200, action } = {}) {
  const container = document.getElementById("toasts");
  if (!container) return () => {};

  const node = el("div", { class: `toast toast--${kind}` },
    icon(TOAST_ICONS[kind] ?? "info", 16),
    el("div", { class: "grow" }, message),
    action ? el("button", {
      class: "btn btn--sm btn--ghost",
      onClick: () => { action.onClick?.(); dismiss(); },
    }, action.label) : null,
    el("button", {
      class: "btn btn--icon btn--ghost btn--sm",
      "aria-label": "Schließen",
      onClick: () => dismiss(),
    }, icon("x", 14)),
  );

  let dismissed = false;
  function dismiss() {
    if (dismissed) return;
    dismissed = true;
    node.style.transition = "opacity 160ms, transform 160ms";
    node.style.opacity = "0";
    node.style.transform = "translateX(12px)";
    setTimeout(() => node.remove(), 170);
  }

  container.append(node);
  if (timeout > 0) setTimeout(dismiss, timeout);
  return dismiss;
}

/* --- dialogs -------------------------------------------------------------------------- */

export function openDialog({ title, body, actions = [], variant = "", dismissible = true }) {
  const root = document.getElementById("dialog-root");
  const backdrop = el("div", { class: "dialog-backdrop" });
  const dialog = el("div", {
    class: `dialog ${variant ? `dialog--${variant}` : ""}`.trim(),
    role: "dialog",
    "aria-modal": "true",
    "aria-label": title,
  });

  function close(result) {
    document.removeEventListener("keydown", onKey);
    backdrop.remove();
    resolve?.(result);
  }

  let resolve;
  const promise = new Promise((r) => { resolve = r; });

  function onKey(event) {
    if (event.key === "Escape" && dismissible) close(null);
  }

  dialog.append(
    el("div", { class: "dialog__head" }, el("h3", { class: "dialog__title" }, title)),
    el("div", { class: "dialog__body" }, body),
    el("div", { class: "dialog__foot" },
      ...actions.map((action) => el("button", {
        class: `btn ${action.class ?? ""}`.trim(),
        onClick: () => close(action.value),
      }, action.label)),
    ),
  );

  backdrop.append(dialog);
  backdrop.addEventListener("click", (event) => {
    if (event.target === backdrop && dismissible) close(null);
  });
  document.addEventListener("keydown", onKey);
  root.append(backdrop);
  dialog.querySelector("button")?.focus();

  return { promise, close };
}

export function confirmDialog(title, message, { confirmLabel = "Bestätigen", variant = "" } = {}) {
  return openDialog({
    title,
    variant,
    body: el("p", { class: "soft" }, message),
    actions: [
      { label: "Abbrechen", value: false, class: "btn--ghost" },
      { label: confirmLabel, value: true, class: variant === "risk" ? "btn--danger" : "btn--primary" },
    ],
  }).promise;
}

/* --- permission prompt (Spec §69) ------------------------------------------------------ */

const RISK_LABELS = {
  SAFE_READ: ["Lesend", "pill--ok"],
  SAFE_ACTION: ["Sichere Aktion", "pill--ok"],
  WRITE: ["Schreibend", "pill--warn"],
  EXTERNAL_WRITE: ["Nach außen wirkend", "pill--warn"],
  SYSTEM_CONTROL: ["Systemsteuerung", "pill--warn"],
  DESTRUCTIVE: ["Zerstörend", "pill--danger"],
  PRIVILEGED: ["Administratorrechte", "pill--danger"],
};

export function permissionDialog(request, { onAnswer }) {
  const [riskLabel, riskClass] = RISK_LABELS[request.risk] ?? ["Unbekannt", "pill--warn"];
  const dangerous = ["DESTRUCTIVE", "PRIVILEGED"].includes(request.risk);

  const argumentList = Object.entries(request.arguments ?? {});
  const body = el("div", { class: "col" },
    el("p", {}, "JARVIS möchte Folgendes tun:"),
    el("div", { class: "card", style: { padding: "12px" } },
      el("div", { class: "mono small", style: { overflowWrap: "anywhere" } },
        request.summary || request.tool),
    ),
    el("dl", { class: "kv" },
      el("dt", {}, "Werkzeug"), el("dd", { class: "mono" }, request.tool),
      el("dt", {}, "Risiko"), el("dd", {},
        el("span", { class: `pill ${riskClass}` }, riskLabel)),
      el("dt", {}, "Berechtigung"), el("dd", { class: "mono" }, request.capability),
      request.scope ? el("dt", {}, "Bereich") : null,
      request.scope ? el("dd", { class: "mono", style: { overflowWrap: "anywhere" } },
        request.scope) : null,
    ),
    argumentList.length
      ? el("details", {},
          el("summary", { class: "small muted" }, "Argumente anzeigen"),
          el("pre", { class: "small", style: { overflowX: "auto", marginTop: "8px" } },
            JSON.stringify(request.arguments, null, 2)),
        )
      : null,
    dangerous
      ? el("p", { class: "small", style: { color: "var(--danger)" } },
          "Diese Aktion lässt sich nicht ohne Weiteres rückgängig machen.")
      : null,
  );

  const handle = openDialog({
    title: "Bestätigung erforderlich",
    variant: dangerous ? "risk" : "",
    dismissible: false,
    body,
    actions: [
      { label: "Ablehnen", value: "deny", class: "btn--ghost" },
      { label: `Immer erlauben${request.scope ? " (dieser Bereich)" : ""}`, value: "allow_always" },
      { label: "Einmal erlauben", value: "allow_once", class: "btn--primary" },
    ],
  });

  handle.promise.then((answer) => onAnswer(answer ?? "deny"));
  return handle;
}

/* --- small building blocks ------------------------------------------------------------- */

export function statPill(label, value, kind = "") {
  return el("span", { class: `pill ${kind}` }, el("span", { class: "muted" }, label), String(value));
}

export function emptyState(iconName, title, description, action = null) {
  return el("div", { class: "empty" },
    el("div", { class: "empty__icon" }, icon(iconName, 34)),
    el("strong", {}, title),
    description ? el("div", { class: "small muted", style: { maxWidth: "46ch" } }, description) : null,
    action,
  );
}

export function sectionCard(title, ...children) {
  return el("section", { class: "card card--lit" },
    el("h3", { class: "card__title" }, title),
    ...children,
  );
}

export function fieldRow(label, control, hint = "") {
  return el("label", { class: "field" },
    el("span", { class: "field__label" }, label),
    control,
    hint ? el("span", { class: "field__hint" }, hint) : null,
  );
}

export function switchControl(checked, onChange, label = "") {
  const input = el("input", { type: "checkbox", checked, onChange: (e) => onChange(e.target.checked) });
  return el("label", { class: "switch" },
    input,
    el("span", { class: "switch__track" }, el("span", { class: "switch__thumb" })),
    label ? el("span", {}, label) : null,
  );
}

export function segmented(values, current, onChange) {
  const group = el("div", { class: "segmented", role: "group" });
  for (const value of values) {
    const label = typeof value === "string" ? value : value.label;
    const key = typeof value === "string" ? value : value.value;
    group.append(el("button", {
      type: "button",
      dataset: { value: key },
      "aria-pressed": String(key === current),
      onClick: () => onChange(key),
    }, label));
  }
  return group;
}

export function renderHtmlSafely(node, markup) {
  // Only ever called with markup this application itself produced.
  clear(node);
  node.innerHTML = markup;
  return node;
}

export { escapeHtml };
