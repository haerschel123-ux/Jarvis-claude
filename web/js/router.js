/* Hash-based routing. No build step, no history-API server rules needed. */

import { el } from "./dom.js";

export const ROUTES = [
  { key: "dashboard", label: "Übersicht", icon: "dashboard" },
  { key: "chat", label: "Chat", icon: "chat" },
  { key: "projects", label: "Projekte", icon: "projects" },
  { key: "tasks", label: "Aufgaben", icon: "tasks" },
  { key: "memory", label: "Gedächtnis", icon: "memory" },
  { key: "automations", label: "Automationen", icon: "automations" },
  { group: "System" },
  { key: "models", label: "Modelle", icon: "cpu" },
  { key: "tools", label: "Werkzeuge", icon: "bolt" },
  { key: "integrations", label: "Integrationen", icon: "integrations" },
  { key: "settings", label: "Einstellungen", icon: "settings" },
];

const handlers = new Map();
let current = "";

export function onRoute(key, handler) {
  handlers.set(key, handler);
}

export function currentRoute() {
  const raw = location.hash.replace(/^#\/?/, "").split("?")[0];
  return raw || "dashboard";
}

export function navigate(key) {
  if (currentRoute() === key) {
    dispatch();
    return;
  }
  location.hash = `#/${key}`;
}

export function dispatch() {
  const key = currentRoute();
  const handler = handlers.get(key) ?? handlers.get("dashboard");
  current = key;
  document.querySelectorAll(".nav__item").forEach((item) => {
    if (item.dataset.route === key) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  const view = document.getElementById("view");
  if (view && handler) {
    view.className = "view";
    handler(view, key);
  }
  // On a phone the navigation is a drawer; close it after picking a destination.
  if (window.innerWidth <= 760) document.getElementById("app")?.removeAttribute("data-nav");
}

export function startRouter() {
  window.addEventListener("hashchange", dispatch);
  if (!location.hash) location.hash = "#/dashboard";
  else dispatch();
}

export function buildNav(iconFactory, onNavigate) {
  const nav = document.getElementById("nav");
  if (!nav) return;
  nav.replaceChildren();

  for (const route of ROUTES) {
    if (route.group) {
      nav.append(el("div", { class: "nav__group-label" }, route.group));
      continue;
    }
    nav.append(el("button", {
      class: "nav__item",
      dataset: { route: route.key },
      onClick: () => onNavigate(route.key),
    },
      iconFactory(route.icon, 18, "nav__icon"),
      el("span", { class: "grow" }, route.label),
      el("span", { class: "nav__badge hidden", dataset: { badge: route.key } }),
    ));
  }

  nav.append(el("div", { class: "nav__footer" },
    el("button", {
      class: "btn btn--block",
      onClick: () => onNavigate("chat", { fresh: true }),
    }, iconFactory("plus", 15), "Neue Unterhaltung"),
  ));
}

export function setNavBadge(routeKey, value) {
  const badge = document.querySelector(`[data-badge="${routeKey}"]`);
  if (!badge) return;
  badge.textContent = value ? String(value) : "";
  badge.classList.toggle("hidden", !value);
}

export { current };
