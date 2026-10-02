// Lot 37 — Bob installable. Aucune donnée n'est gardée sur l'appareil : tout vient du serveur à
// chaque fois (jamais de conversation ni de client en cache). Seule la page « Pas de connexion »
// est mise de côté, pour s'afficher quand le réseau manque.
const OFFLINE_CACHE = "bob-offline-v1";
const OFFLINE_URL = "offline.html";

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(OFFLINE_CACHE).then((cache) => cache.add(OFFLINE_URL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== OFFLINE_CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  // Seules les ouvertures de page sont concernées ; les appels à l'API passent toujours par le réseau.
  if (event.request.mode !== "navigate") return;
  event.respondWith(fetch(event.request).catch(() => caches.match(OFFLINE_URL)));
});

// Lot 37b — notification reçue (même Bob fermé) : message + pastille rouge avec le nombre de
// tâches à faire. Le texte est général : jamais le nom ni le message d'un client.
self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = {}; }
  const count = Number.isInteger(data.count) && data.count >= 0 ? data.count : null;
  const title = typeof data.title === "string" && data.title ? data.title.slice(0, 80) : "Bob";
  const body = typeof data.body === "string" ? data.body.slice(0, 160) : "Nouvelle tâche à traiter.";
  const badge = count === null ? Promise.resolve()
    : count > 0 && self.navigator.setAppBadge ? self.navigator.setAppBadge(count).catch(() => {})
    : self.navigator.clearAppBadge ? self.navigator.clearAppBadge().catch(() => {}) : Promise.resolve();
  event.waitUntil(Promise.all([
    badge,
    self.registration.showNotification(title, {
      body, tag: "bob-tasks", renotify: true, icon: "icon-192.png", badge: "icon-192.png", data: { url: "./" },
    }),
  ]));
});

// Clic sur la notification : on revient sur Bob (onglet déjà ouvert, sinon nouvelle fenêtre).
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = new URL("./", self.registration.scope).href;
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
      const open = windows.find((w) => w.url.startsWith(target));
      if (open) {
        open.postMessage({ type: "bob-refresh-counts" });
        return open.focus();
      }
      return self.clients.openWindow(target);
    })
  );
});
