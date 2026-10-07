/* Zenith Pay — service worker kill switch.
 *
 * An earlier iteration of this app registered a service worker that no
 * longer exists. Browsers keep that stale registration (and its caches)
 * until they are told otherwise, and a stale worker can keep serving old
 * pages even after the site has changed.
 *
 * This file replaces the stale worker with one that removes itself and
 * every cache it owns, then leaves the network in charge. If a real
 * offline-capable worker is added later, it replaces this one.
 */
self.addEventListener('install', () => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      await self.registration.unregister();

      const cacheNames = await caches.keys();
      await Promise.all(cacheNames.map((name) => caches.delete(name)));

      const clients = await self.clients.matchAll({ type: 'window' });
      clients.forEach((client) => {
        client.navigate(client.url).catch(() => {});
      });
    })()
  );
});
