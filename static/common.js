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
