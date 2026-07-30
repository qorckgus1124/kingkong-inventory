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

// ---------- API (재시도 + 지수 백오프) ----------
async function api(url, options, retries = 3) {
  const timeoutMs = 10000;
  for (let i = 0; i < retries; i++) {
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
      if (!res.ok) {
        const msg = (data && data.error) ? data.error : '오류가 발생했습니다.';
        toast(msg, true, '❌');
        throw new Error(msg);
      }
      return data;
    } catch (e) {
      if (e.name === 'AbortError') {
        console.warn(`⏱️ API 타임아웃 (${i+1}/${retries}): ${url}`);
        if (i === retries - 1) {
          toast('서버 응답이 너무 느립니다. 다시 시도해주세요.', true, '⚠️');
          return {};
        }
      } else {
        console.error(`❌ API 오류 (${i+1}/${retries}):`, e);
      }
      if (i === retries - 1) {
        toast('데이터를 불러오지 못했습니다.', true, '⚠️');
        return {};
      }
      // 지수 백오프: 500ms, 1000ms, 2000ms
      await new Promise(r => setTimeout(r, 500 * Math.pow(2, i)));
    }
  }
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
async function downloadFile(url, fallbackName) {
  try {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) {
      let msg = '다운로드 중 오류가 발생했습니다.';
      try {
        const data = await res.clone().json();
        if (data && data.error) msg = data.error;
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
    } else if (plainMatch && plainMatch[1]) {
      filename = plainMatch[1];
    }
    const blobUrl = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = blobUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => window.URL.revokeObjectURL(blobUrl), 2000);
    return true;
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

function matchesSearch(text, query) {
  if (!query) return true;
  return normalizeSearchText(text).includes(normalizeSearchText(query));
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
