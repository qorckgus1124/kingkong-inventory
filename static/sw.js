// 서비스 워커 - 오프라인 캐싱 + PWA 앱 셸 프리캐시
// v6: 정적 파일(css/js/아이콘)은 "캐시 우선"으로 바꿔 화면 전환 체감 속도를 높였다.
//     주소에 ?v=<버전>이 붙어 있어(app.py의 asset_version) 파일이 바뀌면 주소가 바뀌므로,
//     캐시 우선으로 써도 오래된 파일이 계속 쓰이는 문제는 생기지 않는다.
//     페이지 이동/API 요청은 예전처럼 항상 네트워크를 먼저 사용한다(항상 최신 데이터).
// (v1은 캐시를 무한정 붙잡아 배포가 반영되지 않았고, v2에서 네트워크 우선으로 바꿨으며,
//  v3에서 오프라인 안내 페이지를 추가, v5에서 다운로드 로직 갱신)
const CACHE_NAME = 'inventory-v6';
const urlsToCache = [
  '/static/style.css',
  '/static/common.js',
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
      // 일부 파일이 없어도(404) 설치가 실패하지 않도록 개별로 처리한다.
      // (Cache.add는 지원하지 않는 브라우저가 있어 fetch + put으로 처리)
      return Promise.all(urlsToCache.map(function (url) {
        return fetch(new Request(url, { cache: 'reload' }))
          .then(function (res) { if (res && res.status === 200) return cache.put(url, res); })
          .catch(function () {});
      }));
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

function isStaticAsset(url) {
  return url.origin === self.location.origin && url.pathname.startsWith('/static/');
}

self.addEventListener('fetch', function (event) {
  if (event.request.method !== 'GET') return;

  const url = new URL(event.request.url);

  // ---------- 정적 파일: 캐시 우선 (없으면 받아서 캐시에 저장) ----------
  if (isStaticAsset(url)) {
    event.respondWith(
      caches.match(event.request).then(function (cached) {
        if (cached) return cached;
        return fetch(event.request).then(function (response) {
          if (response && response.status === 200) {
            const cloned = response.clone();
            caches.open(CACHE_NAME).then(function (cache) { cache.put(event.request, cloned); });
          }
          return response;
        }).catch(function () {
          // 버전 파라미터만 다른 같은 파일이 캐시에 있으면 그것으로 대체한다.
          return caches.match(event.request, { ignoreSearch: true }).then(function (fallback) {
            return fallback || Response.error();
          });
        });
      })
    );
    return;
  }

  // ---------- 그 외(페이지 이동/API): 네트워크 우선 ----------
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
