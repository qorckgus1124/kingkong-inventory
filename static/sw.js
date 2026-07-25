// 서비스 워커 - 오프라인 캐싱
const CACHE_NAME = 'inventory-v1';
const urlsToCache = [
  '/',
  '/static/style.css',
  '/static/common.js'
];

self.addEventListener('install', function(event) {
  event.waitUntil(
    caches.open(CACHE_NAME).then(function(cache) {
      return cache.addAll(urlsToCache);
    })
  );
});

self.addEventListener('fetch', function(event) {
  event.respondWith(
    caches.match(event.request).then(function(response) {
      return response || fetch(event.request);
    })
  );
});