// ---------- 토스트 (아이콘 중복 제거 완료) ----------
function toast(msg, isError, icon) {
  const el = document.getElementById('toast');
  const icons = {
    success: '✅',
    error: '❌',
    warning: '⚠️',
    info: 'ℹ️'
  };
  const iconChar = icon || (isError ? icons.error : icons.success);
  // 메시지에서 이미 포함된 이모지 제거 (중복 방지)
  const cleanMsg = msg.replace(new RegExp(iconChar, 'g'), '').trim();
  el.innerHTML = `<span class="toast-icon">${iconChar}</span> ${cleanMsg}`;
  el.className = isError ? 'show error' : 'show';
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => { el.className = ''; }, 3000);
}

// ---------- API 실패값 ----------
// 실패했을 때 {}를 돌려주면, 목록을 기대하는 화면들이 곧바로 res.map(...)을 호출하면서
// "map is not a function" 오류로 페이지 스크립트 전체가 멈춰버린다(로딩 중 상태로 굳음).
// 그래서 실패값은 "빈 배열"로 준다. 배열은 .map/.forEach는 물론 res.ok 같은 속성 접근도
// undefined로 안전하게 처리되기 때문에 두 형태(목록/객체) 모두에 안전하다.
// 실패 여부를 구분해야 하는 화면은 결과의 __apiError 값을 확인하면 된다.
function apiFailure(message) {
  const empty = [];
  try { empty.__apiError = message || true; } catch (e) {}
  return empty;
}

// ---------- API (네트워크 오류만 재시도 + 지수 백오프) ----------
async function api(url, options, retries = 3) {
  const timeoutMs = 10000;
  const method = ((options && options.method) || 'GET').toUpperCase();
  // 쓰기 요청(POST/PUT/DELETE)은 재시도하지 않는다. 서버에서 이미 처리된 요청을
  // 다시 보내면 판매/입출고가 중복 등록될 수 있기 때문이다.
  const maxAttempts = method === 'GET' ? retries : 1;
  if (method !== 'GET') {
    // 데이터가 바뀌면 검색/매장 캐시는 즉시 버린다 (오래된 결과가 보이지 않도록)
    try { clearSearchCache(); } catch (e) {}
    try { clearStoreListCache(); } catch (e) {}
  } else if (url === '/api/stores') {
    // 매장 목록은 저장해둔 값이 있으면 기다리지 않고 바로 돌려준다
    const cached = readStoreListCache(STORE_LIST_TTL_MS);
    if (cached) return cached;
  }

  for (let i = 0; i < maxAttempts; i++) {
    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
      const res = await fetch(url, Object.assign({
        headers: { 'Content-Type': 'application/json' },
        signal: controller.signal,
        cache: 'no-store'
      }, options));
      clearTimeout(timeoutId);
      let data = null;
      try { data = await res.json(); } catch (e) {}

      // 서버가 응답을 준 경우(400/404/500 등)는 재시도하지 않는다.
      // 예전에는 여기서 예외를 던져 재시도 루프를 돌았기 때문에, 같은 오류 메시지가
      // 3번 뜨고 같은 요청이 3번 더 전송됐다.
      if (!res.ok) {
        const msg = (data && data.error) ? data.error : '오류가 발생했습니다.';
        toast(msg, true, '❌');
        return apiFailure(msg);
      }
      if (data === null || data === undefined) {
        toast('서버 응답을 읽지 못했습니다.', true, '⚠️');
        return apiFailure('빈 응답');
      }
      if (method === 'GET' && url === '/api/stores') writeStoreListCache(data);
      // 판매/입출고 등 데이터가 바뀌는 요청이 성공하면 "오늘 매출"을 즉시 새로 불러온다.
      // (플로팅 패널과 대시보드 오늘 카드가 실시간으로 보이게 하는 부분)
      if (method !== 'GET') scheduleTodayRevenueRefresh();
      return data;
    } catch (e) {
      // 여기까지 오는 것은 네트워크 단절/타임아웃 같은 전송 실패뿐이다.
      if (e.name === 'AbortError') {
        console.warn(`⏱️ API 타임아웃 (${i + 1}/${maxAttempts}): ${url}`);
        if (i === maxAttempts - 1) {
          toast('서버 응답이 너무 느립니다. 다시 시도해주세요.', true, '⚠️');
          return apiFailure('타임아웃');
        }
      } else {
        console.error(`❌ API 통신 오류 (${i + 1}/${maxAttempts}):`, e);
        if (i === maxAttempts - 1) {
          toast('데이터를 불러오지 못했습니다. 네트워크 상태를 확인해주세요.', true, '⚠️');
          return apiFailure('통신 오류');
        }
      }
      // 지수 백오프: 500ms, 1000ms, 2000ms
      await new Promise(r => setTimeout(r, 500 * Math.pow(2, i)));
    }
  }
  return apiFailure('요청 실패');
}

// ---------- 유틸리티 ----------
function fmt(n) {
  if (n === null || n === undefined) return '-';
  return Number(n).toLocaleString('ko-KR');
}

// "2026-07-28 10:15:23.123" / "2026-07-28T10:15:23" 등 다양한 형태를
// "2026-07-28" (날짜만) 또는 "2026-07-28 10:15" (날짜+시간)으로 통일해서 보여준다.
function fmtDate(dt) {
  if (!dt) return '-';
  const m = String(dt).match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (m) return `${m[1]}-${m[2]}-${m[3]}`;
  return String(dt);
}

function fmtDateTime(dt) {
  if (!dt) return '-';
  const m = String(dt).match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
  if (m) return `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}`;
  const d = String(dt).match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (d) return `${d[1]}-${d[2]}-${d[3]}`;
  return String(dt);
}

function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ---------- 파일 다운로드 (엑셀/CSV 내보내기 공통) ----------
// window.open(url, '_blank')는 모바일 브라우저나 홈 화면에 추가한 PWA(독립 실행 모드)에서
// 새 창을 제대로 열지 못하거나, 서버가 에러(JSON)를 반환해도 사용자가 알아챌 방법이 없어
// "다운로드가 안 된다"는 문제의 흔한 원인이다. fetch로 직접 받아 Blob으로 저장하면
// 실패 시 에러 메시지를 바로 보여줄 수 있고, PWA/모바일 환경에서도 안정적으로 동작한다.
async function downloadFile(url, fallbackName, options) {
  try {
    const opts = options || {};
    // 캐시 무효화용 파라미터: 프록시/서비스워커가 예전 응답을 재사용하지 못하게 한다.
    const bustedUrl = url + (url.includes('?') ? '&' : '?') + '_ts=' + Date.now();
    const headers = Object.assign({ 'Accept': 'text/csv, application/json' }, opts.headers || {});
    if (opts.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
    const res = await fetch(bustedUrl, {
      method: opts.method || 'GET',
      body: opts.body,
      cache: 'no-store',
      credentials: 'same-origin',
      headers: headers,
    });

    const contentType = (res.headers.get('Content-Type') || '').toLowerCase();

    // 서버가 오류를 냈거나, 파일 대신 JSON/HTML(오류 페이지)을 돌려준 경우를 모두 잡는다.
    // (예전에는 이 경우에도 그 내용을 그대로 .csv 파일로 저장해버려서, 사용자에게는
    //  "다운로드는 됐는데 열면 이상한 파일"이거나 아무 반응 없는 것처럼 보였다.)
    if (!res.ok || contentType.includes('application/json') || contentType.includes('text/html')) {
      let msg = `다운로드 중 오류가 발생했습니다. (${res.status})`;
      try {
        const text = await res.clone().text();
        if (contentType.includes('application/json')) {
          const data = JSON.parse(text);
          if (data && data.error) msg = data.error;
        }
      } catch (e) {}
      toast(msg, true, '❌');
      return false;
    }

    const blob = await res.blob();
    if (!blob || blob.size === 0) {
      toast('다운로드할 내용이 없습니다.', true, '⚠️');
      return false;
    }

    let filename = fallbackName || 'download.csv';
    const disposition = res.headers.get('Content-Disposition') || '';
    const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
    const plainMatch = disposition.match(/filename="?([^";]+)"?/i);
    if (utf8Match && utf8Match[1]) {
      try { filename = decodeURIComponent(utf8Match[1]); } catch (e) {}
    } else if (plainMatch && plainMatch[1] && plainMatch[1] !== '.csv') {
      filename = plainMatch[1];
    }

    const blobUrl = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    const supportsDownload = 'download' in a;
    if (supportsDownload) {
      a.href = blobUrl;
      a.download = filename;
      a.rel = 'noopener';
      document.body.appendChild(a);
      a.click();
      a.remove();
    } else {
      // iOS Safari 등 download 속성을 지원하지 않는 환경: 새 탭으로 열어 사용자가 저장하게 한다.
      window.open(blobUrl, '_blank');
      toast('다운로드가 지원되지 않는 브라우저입니다. 열린 화면에서 저장해주세요.', false, 'ℹ️');
    }
    setTimeout(() => window.URL.revokeObjectURL(blobUrl), 10000);

    // 성공 시 { ok: true, ... } (객체이므로 기존 if (결과) 검사도 그대로 동작한다)
    const rowCountHeader = res.headers.get('X-Row-Count');
    const rowCount = rowCountHeader === null ? null : parseInt(rowCountHeader, 10);
    return { ok: true, filename, rowCount, size: blob.size };
  } catch (e) {
    console.error('❌ 다운로드 오류:', e);
    toast('다운로드 중 오류가 발생했습니다. 네트워크 상태를 확인해주세요.', true, '⚠️');
    return false;
  }
}

// ---------- 복사 기능 (강화 완료) ----------
function copyText(textarea) {
  if (!textarea) {
    toast('복사할 내용이 없습니다.', true, '⚠️');
    return;
  }

  const text = textarea.value;
  if (!text || text.trim() === '') {
    toast('복사할 내용이 비어있습니다.', true, '⚠️');
    return;
  }

  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text)
      .then(() => {
        toast('복사되었습니다!', false, '✅');
      })
      .catch(() => {
        fallbackCopy(textarea);
      });
  } else {
    fallbackCopy(textarea);
  }
}

function fallbackCopy(textarea) {
  const originalReadOnly = textarea.readOnly;
  textarea.readOnly = false;
  textarea.focus();
  textarea.select();
  textarea.setSelectionRange(0, 99999);

  try {
    const success = document.execCommand('copy');
    if (success) {
      toast('복사되었습니다!', false, '✅');
    } else {
      toast('⚠️ 복사에 실패했습니다. 텍스트를 직접 선택해주세요.', true, '⚠️');
    }
  } catch (e) {
    toast('⚠️ 복사에 실패했습니다. 텍스트를 직접 선택해주세요.', true, '⚠️');
  } finally {
    textarea.readOnly = originalReadOnly;
    if (window.getSelection) {
      window.getSelection().removeAllRanges();
    }
  }
}

// ---------- 장바구니 ----------
// localStorage를 사용해 같은 브라우저의 다른 탭/페이지로 이동해도 장바구니가 유지되도록 한다.
// (sessionStorage는 탭마다 독립된 저장소라 새 탭이나 다른 탭으로 옮기면 장바구니가 사라진다.)
function saveCartToStorage(key, cart) {
  try {
    localStorage.setItem(key, JSON.stringify(cart));
  } catch (e) {
    console.warn('장바구니 저장 실패:', e);
  }
}

function loadCartFromStorage(key) {
  try {
    const data = localStorage.getItem(key);
    return data ? JSON.parse(data) : {};
  } catch (e) {
    console.warn('장바구니 복원 실패:', e);
    return {};
  }
}

function clearCartFromStorage(key) {
  try {
    localStorage.removeItem(key);
  } catch (e) {
    console.warn('장바구니 삭제 실패:', e);
  }
}

function clearAllCartsFromStorage() {
  try {
    const keys = ['normal_cart', 'pre_cart', 'transfer_cart'];
    keys.forEach(key => localStorage.removeItem(key));
  } catch (e) {
    console.warn('모든 장바구니 삭제 실패:', e);
  }
}
// ---------------------------------------------------------------------------
// 공백 무시 검색 매칭 (서버 쪽 normalize_search()와 동일한 규칙)
// "드 알" 처럼 검색어에 공백이 있어도, 대상 문자열에서 공백을 제거했을 때
// 검색어(역시 공백 제거)가 "이어진 문자열"로 등장하는 경우에만 일치로 본다.
// 예) "파드 알로에그레이프" -> "드알"을 포함(O), "레드애플 알로에" -> "드알" 미포함(X)
// ---------------------------------------------------------------------------
function normalizeSearchText(s) {
  return (s || '').replace(/\s+/g, '').toLowerCase();
}

// 검색어를 공백 기준 낱말로 나눈다. 예) "밤 백향" -> ["밤", "백향"]
function searchTokens(query) {
  return String(query || '')
    .trim()
    .split(/\s+/)
    .map(t => normalizeSearchText(t))
    .filter(t => t.length > 0);
}

// 낱말이 "전부" 들어있어야 일치로 본다 (서버 검색 규칙과 동일).
// 여러 항목(제품명, 브랜드명 등)을 넘기면 그중 어디에 있어도 인정한다.
//   matchesSearch('백향과', '밤 백향', '펀치밤')  -> true
//   matchesSearch('v21', '2%')                    -> false ("2%"라는 글자가 없으므로)
function matchesSearch(text, query, ...extraTexts) {
  const tokens = searchTokens(query);
  if (tokens.length === 0) return true;
  const haystacks = [text, ...extraTexts].map(t => normalizeSearchText(t));
  return tokens.every(token => haystacks.some(h => h.includes(token)));
}

// ---------------------------------------------------------------------------
// 오늘 매출 즉시 갱신 요청 (base.html의 refreshTodayRevenue를 짧게 묶어서 호출)
// 장바구니를 여러 건 등록하면 요청이 연달아 나가므로, 0.4초 안의 호출은 한 번으로 합친다.
// ---------------------------------------------------------------------------
let _todayRevenueRefreshTimer = null;

function scheduleTodayRevenueRefresh() {
  if (typeof window.refreshTodayRevenue !== 'function') return;
  clearTimeout(_todayRevenueRefreshTimer);
  _todayRevenueRefreshTimer = setTimeout(() => {
    try { window.refreshTodayRevenue(); } catch (e) {}
  }, 400);
}

// ---------------------------------------------------------------------------
// 매장 목록 캐시 (선택한 매장이 "귀속"된 것처럼 유지되게 만드는 핵심)
// ⚡ 매장 목록은 거의 바뀌지 않는데도 화면을 옮길 때마다 여러 번 다시 불러왔다.
//    그 사이 드롭다운이 "매장 선택"으로 비어 보였다가 값이 채워지면서 두 단계로
//    바뀌고, 그 과정에서 목록 조회가 한 번 더 실행돼 느려졌다.
//    목록을 브라우저에 저장해두면 다음 화면에서는 기다림 없이 바로 채워진다.
//    매장을 추가/수정/삭제하면(쓰기 요청) 캐시를 즉시 버린다.
// ---------------------------------------------------------------------------
const STORE_LIST_CACHE_KEY = 'stores_cache_v1';
const STORE_LIST_TTL_MS = 10 * 60 * 1000;   // 10분

function readStoreListCache(maxAgeMs) {
  try {
    const raw = localStorage.getItem(STORE_LIST_CACHE_KEY);
    if (!raw) return null;
    const entry = JSON.parse(raw);
    if (!entry || !Array.isArray(entry.d) || entry.d.length === 0) return null;
    if (maxAgeMs !== undefined && (Date.now() - entry.t) > maxAgeMs) return null;
    return entry.d;
  } catch (e) {
    return null;
  }
}

function writeStoreListCache(list) {
  try {
    if (!Array.isArray(list) || list.length === 0) return;
    localStorage.setItem(STORE_LIST_CACHE_KEY, JSON.stringify({ t: Date.now(), d: list }));
  } catch (e) { /* 저장 공간 문제는 무시 (캐시는 없어도 동작함) */ }
}

function clearStoreListCache() {
  try { localStorage.removeItem(STORE_LIST_CACHE_KEY); } catch (e) {}
}

// 저장해둔 매장 목록을 즉시(네트워크 없이) 돌려준다. 없으면 null.
function getStoresInstant() {
  return readStoreListCache();
}

// ---------------------------------------------------------------------------
// 검색 결과 임시 캐시 (같은 검색어를 다시 치거나 한 글자 지웠다 되돌릴 때 즉시 표시)
// ⚡ 서버 왕복을 건너뛰기 때문에 체감 대기시간이 0에 가까워진다.
//    메모리에만 두고 30초만 유지하며, 데이터가 바뀌는 요청이 나가면 통째로 버린다.
// ---------------------------------------------------------------------------
const SEARCH_CACHE_TTL_MS = 30 * 1000;
const SEARCH_CACHE_MAX = 60;
const _searchCache = new Map();

function clearSearchCache() {
  _searchCache.clear();
}

async function apiSearch(url) {
  const hit = _searchCache.get(url);
  if (hit && (Date.now() - hit.t) < SEARCH_CACHE_TTL_MS) {
    return hit.d;
  }
  const data = await api(url);
  // 정상적으로 받아온 목록만 캐시한다 (오류 응답은 캐시하지 않음).
  if (Array.isArray(data) && !data.__apiError) {
    if (_searchCache.size >= SEARCH_CACHE_MAX) {
      _searchCache.delete(_searchCache.keys().next().value);
    }
    _searchCache.set(url, { t: Date.now(), d: data });
  }
  return data;
}

// ---------------------------------------------------------------------------
// 검색 결과 경쟁 상태(race condition) 방지
// 타이핑할 때마다 새 요청을 보내면, 앞서 보낸 요청(예: "드")이 나중에 보낸
// 요청(예: "드 알")보다 응답 크기가 커서 네트워크에서 더 늦게 도착하는 경우가
// 있다. 이때 아무 안전장치가 없으면 늦게 도착한 "드" 결과가 화면에 먼저 그려진
// "드 알" 결과를 덮어써서, 잠깐 맞게 보이다가 다시 예전 상태로 돌아가는 것처럼
// 보인다. makeSearchGuard()로 만든 가드에 매 요청마다 순번표(token)를 받고,
// 응답이 왔을 때 그 순번표가 여전히 "가장 최근에 보낸 요청"인지 확인해서,
// 아니면(더 최신 요청이 이미 나간 상태라면) 화면 반영을 건너뛴다.
// ---------------------------------------------------------------------------
function makeSearchGuard() {
  let token = 0;
  return {
    next() { return ++token; },
    isCurrent(t) { return t === token; },
  };
}

// ---------------------------------------------------------------------------
// 숫자 입력칸(type="number") 위에서 마우스 휠을 굴렸을 때 값이 실수로
// 증감되는 것을 막는다. 포커스된 number input 위에서 휠 이벤트가 발생하면
// 즉시 포커스를 풀어(blur) 값 변경 없이 페이지가 정상적으로 스크롤되게 한다.
// (모든 페이지에서 공통으로 쓰는 common.js에 한 번만 넣으면 전체 적용된다.)
document.addEventListener('wheel', function (e) {
  const el = document.activeElement;
  if (el && el.tagName === 'INPUT' && el.type === 'number') {
    el.blur();
  }
}, { passive: true });

// ---------------------------------------------------------------------------
// 오프라인 임시 입력 → 온라인 동기화
// 네트워크가 끊긴 상태(또는 요청 실패 시)에도 입력을 잃어버리지 않도록
// localStorage에 임시 큐로 쌓아두고, 온라인 복귀 시(또는 수동 버튼으로) 서버에 순차 전송한다.
// ---------------------------------------------------------------------------
const OFFLINE_QUEUE_KEY = 'offline_action_queue';
let __offlineSyncing = false;

function genClientUuid() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return 'off_' + Date.now() + '_' + Math.random().toString(36).slice(2, 10);
}

function getOfflineQueue() {
  try { return JSON.parse(localStorage.getItem(OFFLINE_QUEUE_KEY) || '[]'); } catch (e) { return []; }
}

function setOfflineQueue(list) {
  try { localStorage.setItem(OFFLINE_QUEUE_KEY, JSON.stringify(list)); } catch (e) { console.warn('오프라인 큐 저장 실패:', e); }
  updateOfflineBanner();
}

function queueOfflineAction(endpoint, method, body, label) {
  const list = getOfflineQueue();
  const payload = Object.assign({}, body || {}, { client_uuid: (body && body.client_uuid) || genClientUuid() });
  const item = {
    id: payload.client_uuid,
    endpoint, method: method || 'POST', body: payload,
    label: label || '', created_at: new Date().toISOString(),
  };
  list.push(item);
  setOfflineQueue(list);
  return item;
}

// 일반 api() 대신 이 함수를 쓰면, 오프라인이거나 네트워크 오류가 나는 순간
// 자동으로 로컬 큐에 임시 저장하고 { queued: true }를 반환한다.
// 서버가 정상 응답하면 평소처럼 결과 데이터를 반환한다.
async function apiOrQueue(endpoint, method, body, label) {
  const payload = Object.assign({}, body || {});
  if (!navigator.onLine) {
    queueOfflineAction(endpoint, method, payload, label);
    toast('오프라인 상태입니다. 온라인이 되면 자동으로 등록됩니다.', false, '📴');
    return { queued: true };
  }
  try {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 8000);
    const res = await fetch(endpoint, {
      method: method || 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal: controller.signal,
      cache: 'no-store',
    });
    clearTimeout(timeoutId);
    let data = null;
    try { data = await res.json(); } catch (e) {}
    if (!res.ok) {
      const msg = (data && data.error) ? data.error : '오류가 발생했습니다.';
      toast(msg, true, '❌');
      return { error: msg };
    }
    return data || {};
  } catch (e) {
    // 네트워크 자체가 끊긴 것으로 보이는 경우에만 큐에 저장 (서버 오류와 구분)
    queueOfflineAction(endpoint, method, payload, label);
    toast('네트워크가 불안정합니다. 임시 저장했어요. 온라인이 되면 자동 등록됩니다.', false, '📴');
    return { queued: true };
  }
}

async function syncOfflineQueue(silent) {
  if (__offlineSyncing) return;
  const list = getOfflineQueue();
  if (!list.length) {
    if (!silent) toast('동기화할 대기 항목이 없습니다.', false, 'ℹ️');
    return;
  }
  if (!navigator.onLine) {
    if (!silent) toast('오프라인 상태에서는 동기화할 수 없습니다.', true, '⚠️');
    return;
  }
  __offlineSyncing = true;
  updateOfflineBanner(true);
  let success = 0, fail = 0;
  const remaining = [];
  for (const item of list) {
    try {
      const res = await fetch(item.endpoint, {
        method: item.method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(item.body),
        cache: 'no-store',
      });
      if (res.ok) {
        success++;
      } else {
        fail++;
        remaining.push(item);
      }
    } catch (e) {
      // 여전히 오프라인/네트워크 오류 -> 다음 기회에 재시도
      remaining.push(item);
    }
  }
  __offlineSyncing = false;
  setOfflineQueue(remaining);
  if (success > 0 || fail > 0) {
    toast(`오프라인 동기화: ${success}건 완료${fail ? `, ${fail}건 재시도 대기` : ''}`, fail > success, fail > 0 ? '⚠️' : '✅');
  }
  if (typeof onOfflineSyncDone === 'function') {
    try { onOfflineSyncDone(); } catch (e) {}
  }
}

function updateOfflineBanner(syncing) {
  const banner = document.getElementById('offlineQueueBanner');
  if (!banner) return;
  const list = getOfflineQueue();
  const countEl = document.getElementById('offlineQueueCount');
  const btn = document.getElementById('offlineQueueSyncBtn');
  if (list.length === 0) {
    banner.style.display = 'none';
    return;
  }
  banner.style.display = 'flex';
  if (countEl) countEl.textContent = list.length;
  if (btn) {
    btn.disabled = !!syncing || !navigator.onLine;
    btn.textContent = syncing ? '동기화 중...' : (navigator.onLine ? '지금 동기화' : '오프라인');
  }
}

window.addEventListener('online', () => {
  toast('온라인 상태로 전환되었습니다. 대기 중인 항목을 동기화합니다.', false, '📶');
  updateOfflineBanner();
  syncOfflineQueue(true);
});
window.addEventListener('offline', () => {
  toast('오프라인 상태입니다. 입력한 내용은 임시 저장되며, 온라인이 되면 자동 등록됩니다.', true, '📴');
  updateOfflineBanner();
});
document.addEventListener('DOMContentLoaded', () => {
  updateOfflineBanner();
  if (navigator.onLine) syncOfflineQueue(true);
});
