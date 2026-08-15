// Minimal network-first service worker so the app is installable to a phone
// home screen. Keeps things simple: always try the network, fall back to a
// tiny cache for the shell if offline.
const CACHE = 'helioops-v1';
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => self.clients.claim());
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  e.respondWith(
    fetch(e.request)
      .then(r => { const c = r.clone(); caches.open(CACHE).then(x => x.put(e.request, c)); return r; })
      .catch(() => caches.match(e.request))
  );
});
