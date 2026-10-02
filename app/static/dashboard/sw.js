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
