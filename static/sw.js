// 서비스 워커 - 오프라인 캐싱 + PWA 앱 셸 프리캐시
// v3: 홈 화면 추가(PWA) 최적화 - 아이콘/매니페스트 프리캐시 + 오프라인 폴백 페이지 추가.
// (v1은 캐시를 무한정 붙잡고 있어서, 서버를 배포해도 브라우저가 옛날 페이지를
//  계속 보여주는 문제가 있었음. v2에서 네트워크 우선 전략으로 변경했고,
//  v3에서는 완전히 오프라인일 때 빈 화면 대신 안내 페이지를 보여주도록 개선)
// v5: 다운로드(엑셀/CSV 내보내기) 로직이 담긴 common.js가 바뀌었으므로 캐시 이름을 올려
//     예전 캐시를 폐기한다 (activate 단계에서 이름이 다른 캐시는 모두 삭제된다).
const CACHE_NAME = 'inventory-v5';
const urlsToCache = [
  '/static/style.css',
  '/static/common.js?v=5',
  '/static/manifest.json',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/offline.html',
  '/quick_io',
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
// 페이지 이동(navigation) 요청이고 캐시에도 없으면, 빈 화면 대신
// 안내용 오프라인 페이지(offline.html)를 보여준다.
self.addEventListener('fetch', function (event) {
  if (event.request.method !== 'GET') return;

  event.respondWith(
    fetch(event.request)
      .then(function (response) {
        // 오프라인 임시 입력을 위해, 정상적으로 불러온 페이지는 동적으로 캐시해둔다.
        // (한 번이라도 온라인 상태에서 방문한 페이지는 이후 오프라인에서도 열 수 있게 됨)
        if (event.request.mode === 'navigate' && response && response.status === 200) {
          const cloned = response.clone();
          caches.open(CACHE_NAME).then(function (cache) { cache.put(event.request, cloned); });
        }
        return response;
      })
      .catch(function () {
        // ignoreSearch: common.js?v=5 처럼 캐시 무효화용 쿼리스트링이 붙어도
        // 오프라인일 때 캐시된 같은 파일을 찾아 쓸 수 있게 한다.
        return caches.match(event.request, { ignoreSearch: true }).then(function (cached) {
          if (cached) return cached;
          if (event.request.mode === 'navigate') {
            return caches.match('/static/offline.html');
          }
          return Response.error();
        });
      })
  );
});
