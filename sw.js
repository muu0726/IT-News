/**
 * Service Worker — IT Info Hub
 * HTML/JSON: Network First（常に最新優先・オフライン時のみキャッシュ）
 * その他静的アセット: Cache First
 */

const CACHE_NAME = 'itinfohub-v11';

const PRECACHE_URLS = [
  './',
  './index.html',
  './gadget.html',
  './manifest.json',
];

// Install: precache core files
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      console.log('[SW] Precaching core assets');
      return cache.addAll(PRECACHE_URLS);
    })
  );
  self.skipWaiting();
});

// Activate: clean old caches
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key !== CACHE_NAME)
          .map((key) => caches.delete(key))
      )
    )
  );
  self.clients.claim();
});

// ネットワーク優先（クエリを除外したキーでキャッシュ更新、失敗時はキャッシュへフォールバック）
function networkFirst(request, { skipCache = false } = {}) {
  const url = new URL(request.url);
  const cacheKey = new Request(url.origin + url.pathname);
  return fetch(request)
    .then((response) => {
      if (!skipCache && response.ok) {
        const clone = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(cacheKey, clone));
      }
      return response;
    })
    .catch(async () => {
      if (!skipCache) {
        const cached = await caches.match(cacheKey);
        if (cached) return cached;
      }
      return new Response('Offline', { status: 503, statusText: 'Service Unavailable' });
    });
}

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);
  if (!url.protocol.startsWith('http')) return;

  // 大容量の全期間検索インデックス・日次個別アーカイブはキャッシュ肥大防止のため Network Only (skipCache)
  const isLargeArchiveData =
    url.pathname.endsWith('/archive/search_index.json') ||
    /\/archive\/\d{4}-\d{2}-\d{2}\.json$/.test(url.pathname);

  // HTML（ナビゲーション）と JSON/XML データは Network First
  // → フロント更新やデータ更新が即時反映される
  if (
    event.request.mode === 'navigate' ||
    url.pathname.endsWith('.html') ||
    url.pathname.endsWith('.json') ||
    url.pathname.endsWith('.xml')
  ) {
    event.respondWith(networkFirst(event.request, { skipCache: isLargeArchiveData }));
    return;
  }

  // 静的アセットは Cache First
  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request)
        .then((response) => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
          }
          return response;
        })
        .catch(() => new Response('', { status: 503, statusText: 'Service Unavailable' }));
    })
  );
});
