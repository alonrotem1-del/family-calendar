// Opens the app at once from this phone, as an app should: the page comes
// from the network when the server answers quickly, and from the saved copy
// when it doesn't (Render's free server can take half a minute to wake).
// Bump VERSION when the static files change.
const VERSION = "v15";
const DATA = "data"; // not versioned: the last data survives app updates
const WAKE_WAIT_MS = 1500;
const SHELL = [
  "/",
  "/manifest.webmanifest",
  "/icons/icon.svg",
  "/icons/icon-192.png",
];
const DATA_PATHS = ["/api/events", "/api/people", "/api/school", "/api/school/notes"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open("shell-" + VERSION).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== DATA && !k.endsWith(VERSION)).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

function fromNetwork(request, cacheName) {
  return fetch(request).then((response) => {
    if (response.ok) {
      const copy = response.clone();
      caches.open(cacheName).then((c) => c.put(request, copy));
    }
    return response;
  });
}

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin) return;

  if (DATA_PATHS.includes(url.pathname)) {
    // The app shows the saved answer itself while waiting; this keeps it fresh.
    event.respondWith(
      fromNetwork(event.request, DATA).catch(() =>
        caches.match(event.request).then((r) => r || Response.error())
      )
    );
    return;
  }

  if (url.pathname.startsWith("/api/")) return;

  // "/app.js?v=3f2a…": named by its content, so a saved copy is always the right one.
  if (url.searchParams.has("v")) {
    event.respondWith(
      caches.match(event.request).then((r) => r || fromNetwork(event.request, "shell-" + VERSION))
    );
    return;
  }

  // App files: the network if it answers within a moment, else the saved copy.
  // Either way the network answer, when it comes, updates the saved copy.
  const network = fromNetwork(event.request, "shell-" + VERSION);
  event.waitUntil(network.catch(() => {}));
  event.respondWith(
    (async () => {
      const cached =
        (await caches.match(event.request)) ||
        (event.request.mode === "navigate" ? await caches.match("/") : undefined);
      if (!cached) return network;
      const slow = new Promise((resolve) => setTimeout(() => resolve(cached), WAKE_WAIT_MS));
      return Promise.race([network.catch(() => cached), slow]);
    })()
  );
});
