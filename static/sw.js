// 서비스 워커 - 오프라인 캐싱 + PWA 앱 셸 프리캐시
// v7: 버전을 손으로 올리지 않는다. 서버(/sw.js 라우트)가 __ASSET_VERSION__ 자리에
//     css/js 주소의 ?v= 값과 같은 버전을 넣어 준다. 배포로 파일이 바뀌면 이 파일 내용도
//     바뀌므로 새 서비스워커가 설치되고, 이름이 다른 예전 캐시는 activate에서 지워진다.
//     - 예전에는 common.js?v=5 처럼 버전이 고정돼 있어, 배포 뒤 오프라인이면 옛 JS가 돌 수 있었다.
//     - 로그인이 필요한 /quick_io 를 미리 받아 두면 로그인 전에는 로그인 페이지가 대신 저장됐다.
//       -> 페이지는 미리 받지 않고, 로그인한 상태에서 실제로 열린 페이지만 저장한다.
const VERSION = '__ASSET_VERSION__';
const CACHE_NAME = 'inventory-' + VERSION;
const urlsToCache = [
  '/static/style.css?v=' + VERSION,
  '/static/common.js?v=' + VERSION,
  '/static/immersive.js?v=' + VERSION,
  '/static/base.js?v=' + VERSION,
  '/static/icons3d.svg?v=' + VERSION,
  '/static/manifest.json',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/offline.html',
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

// 저장해도 되는 응답인지: 정상(200)이고, 로그인 페이지로 돌려보내진(redirected) 응답이 아니어야 한다.
function shouldStore(request, response) {
  if (!response || response.status !== 200 || response.redirected) return false;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return false;
  if (url.pathname.startsWith('/static/')) return true;
  if (request.mode !== 'navigate') return false;
  return !(url.pathname === '/login' || url.pathname === '/logout'
    || url.pathname.startsWith('/admin') || url.pathname === '/account');
}

// 네트워크 우선: 온라인이면 항상 최신 버전을 받아오고,
// 오프라인일 때만 저장해 둔 페이지/정적 파일로 대체한다.
// 페이지 이동(navigation) 요청이고 저장된 것도 없으면 안내용 오프라인 페이지(offline.html)를 보여준다.
self.addEventListener('fetch', function (event) {
  if (event.request.method !== 'GET') return;

  event.respondWith(
    fetch(event.request)
      .then(function (response) {
        if (shouldStore(event.request, response)) {
          const cloned = response.clone();
          caches.open(CACHE_NAME).then(function (cache) { cache.put(event.request, cloned); });
        }
        return response;
      })
      .catch(function () {
        // 정적 파일은 ?v= 가 달라도 같은 파일로 본다 (ignoreSearch)
        const isStatic = new URL(event.request.url).pathname.startsWith('/static/');
        return caches.match(event.request, { ignoreSearch: isStatic }).then(function (cached) {
          if (cached) return cached;
          if (event.request.mode === 'navigate') {
            return caches.match('/static/offline.html');
          }
          return Response.error();
        });
      })
  );
});
