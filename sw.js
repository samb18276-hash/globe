const CACHE = 'globe-v1';
const CORE = [
  './globe3.0.html',
  './globe.gl.min.js',
  './leaflet.min.js',
  './leaflet.min.css',
];

// Install: cache core files
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(CORE))
  );
  self.skipWaiting();
});

// Activate: clear old caches
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Fetch: serve from cache, fall back to network, cache new responses
self.addEventListener('fetch', e => {
  // Skip non-GET and cross-origin API calls (flights, weather, etc.)
  if (e.request.method !== 'GET') return;
  const url = new URL(e.request.url);
  const isExternal = url.origin !== self.location.origin;
  const isApi = isExternal && (
    url.hostname.includes('opensky') ||
    url.hostname.includes('open-meteo') ||
    url.hostname.includes('nominatim') ||
    url.hostname.includes('router.project-osrm') ||
    url.hostname.includes('routing.openstreetmap') ||
    url.hostname.includes('restcountries') ||
    url.hostname.includes('raw.githubusercontent') ||
    url.hostname.includes('basemaps.cartocdn') ||
    url.hostname.includes('arcgisonline')
  );

  if (isApi) {
    // Always network for live data — no caching
    e.respondWith(fetch(e.request).catch(() => new Response('', { status: 503 })));
    return;
  }

  // Cache-first for local files and CDN assets (globe textures etc.)
  e.respondWith(
    caches.match(e.request).then(cached => {
      if (cached) return cached;
      return fetch(e.request).then(resp => {
        if (resp && resp.status === 200) {
          const clone = resp.clone();
          caches.open(CACHE).then(c => c.put(e.request, clone));
        }
        return resp;
      }).catch(() => cached || new Response('', { status: 503 }));
    })
  );
});
