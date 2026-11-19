// Opens the app at once from this phone, as an app should: the page comes
// from the network when the server answers quickly, and from the saved copy
// when it doesn't. While Render's free server sleeps, Render answers at once
// with its own "waking up" page; only answers carrying our X-Family-Calendar
// mark are shown or saved. Bump VERSION when this file changes.
const VERSION = "v16";
const DATA = "data"; // not versioned: the last data survives app updates
const SHELL_CACHE = "shell-" + VERSION;
const WAKE_WAIT_MS = 1500;
const SHELL = ["/", "/manifest.webmanifest", "/icons/icon.svg", "/icons/icon-192.png"];
const DATA_PATHS = ["/api/events", "/api/people", "/api/school", "/api/school/notes"];

const ours = (response) => response.headers.get("X-Family-Calendar") === "1";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) =>
      Promise.all(
        SHELL.map((path) =>
          fetch(path, { cache: "no-cache" })
            .then((response) => response.ok && ours(response) && cache.put(path, response))
            .catch(() => {})
        )
      )
    )
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      // Old copies go only once the new one holds the page, so a server asleep
      // during an update never leaves the phone without the app.
      if (await (await caches.open(SHELL_CACHE)).match("/")) {
        const keys = await caches.keys();
        await Promise.all(keys.filter((k) => k !== DATA && k !== SHELL_CACHE).map((k) => caches.delete(k)));
      }
      await self.clients.claim();
    })()
  );
});

/** The server's answer, saved for next time; rejects for Render's waking page. */
function fromNetwork(request, cacheName) {
  return fetch(request).then((response) => {
    if (!ours(response)) throw Object.assign(new Error("server waking up"), { response });
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
    // The page shows the saved answer itself and retries while the server wakes.
    event.respondWith(
      fromNetwork(event.request, DATA).catch(
        () => new Response(JSON.stringify({ detail: "waking" }), { status: 503 })
      )
    );
    return;
  }

  if (url.pathname.startsWith("/api/")) return;

  // "/app.js?v=3f2a…": named by its content, so a saved copy is always the right one.
  if (url.searchParams.has("v")) {
    event.respondWith(
      caches.match(event.request).then((r) => r || fromNetwork(event.request, SHELL_CACHE))
    );
    return;
  }

  // The page: the server if it answers within a moment, else the saved copy.
  const network = fromNetwork(event.request, SHELL_CACHE);
  event.waitUntil(network.catch(() => {}));
  event.respondWith(
    (async () => {
      const cached =
        (await caches.match(event.request)) ||
        (event.request.mode === "navigate" ? await caches.match("/") : undefined);
      // Nothing saved yet (first open): Render's waking page is better than nothing.
      if (!cached) return network.catch((e) => e.response || Response.error());
      const slow = new Promise((resolve) => setTimeout(() => resolve(cached), WAKE_WAIT_MS));
      return Promise.race([network.catch(() => cached), slow]);
    })()
  );
});
