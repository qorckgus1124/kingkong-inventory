// 서비스 워커 - 오프라인 캐싱
// v2: 캐시 버전을 올리고, 예전 캐시를 자동 정리 + "네트워크 우선" 전략으로 변경.
// (v1은 캐시를 무한정 붙잡고 있어서, 서버를 배포해도 브라우저가 옛날 페이지를
//  계속 보여주는 문제가 있었음)
const CACHE_NAME = 'inventory-v2';
const urlsToCache = [
  '/static/style.css',
  '/static/common.js'
];

self.addEventListener('install', function (event) {
  self.skipWaiting(); // 새 서비스워커를 바로 활성화
  event.waitUntil(
    caches.open(CACHE_NAME).then(function (cache) {
      return cache.addAll(urlsToCache);
    })
  );
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(
        keys.filter(function (key) { return key !== CACHE_NAME; })
            .map(function (key) { return caches.delete(key); })
      );
    }).then(function () {
      return self.clients.claim(); // 열려있는 탭에도 새 서비스워커 즉시 적용
    })
  );
});

// 네트워크 우선: 온라인이면 항상 최신 버전을 받아오고,
// 오프라인일 때만 캐시된 정적 파일로 대체한다.
self.addEventListener('fetch', function (event) {
  if (event.request.method !== 'GET') return;

  event.respondWith(
    fetch(event.request)
      .then(function (response) {
        return response;
      })
      .catch(function () {
        return caches.match(event.request);
      })
  );
});
