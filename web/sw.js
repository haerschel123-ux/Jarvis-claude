/* Service worker: cache the shell so the UI opens offline (Spec §74, §84).

   API requests are never cached — a stale model list or health report would be worse than an
   honest error. */

const CACHE = "jarvis-shell-v1";
const SHELL = [
  "/", "/index.html", "/manifest.json",
  "/css/tokens.css", "/css/base.css", "/css/animations.css",
  "/css/layout.css", "/css/components.css", "/css/orb.css",
  "/js/app.js", "/js/api.js", "/js/dom.js", "/js/events.js", "/js/state.js",
  "/js/ui.js", "/js/router.js", "/js/activity.js",
  "/js/views/chat.js", "/js/views/dashboard.js", "/js/views/models.js",
  "/js/views/tools.js", "/js/views/settings.js", "/js/views/placeholder.js",
  "/js/views/memory.js", "/js/views/tasks.js", "/js/views/automations.js",
  "/assets/icons/icon.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
      .catch(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET") return;
  if (url.origin !== location.origin) return;
  // Live data must never come from the cache.
  if (url.pathname.startsWith("/api/")) return;

  event.respondWith(
    caches.match(event.request).then((cached) => {
      const network = fetch(event.request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone();
            caches.open(CACHE).then((cache) => cache.put(event.request, copy));
          }
          return response;
        })
        .catch(() => cached ?? caches.match("/index.html"));
      // Cache first for a fast start, with a background refresh.
      return cached ?? network;
    }),
  );
});
