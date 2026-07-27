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
function saveCartToStorage(key, cart) {
  try {
    sessionStorage.setItem(key, JSON.stringify(cart));
  } catch (e) {
    console.warn('장바구니 저장 실패:', e);
  }
}

function loadCartFromStorage(key) {
  try {
    const data = sessionStorage.getItem(key);
    return data ? JSON.parse(data) : {};
  } catch (e) {
    console.warn('장바구니 복원 실패:', e);
    return {};
  }
}

function clearCartFromStorage(key) {
  try {
    sessionStorage.removeItem(key);
  } catch (e) {
    console.warn('장바구니 삭제 실패:', e);
  }
}

function clearAllCartsFromStorage() {
  try {
    const keys = ['normal_cart', 'pre_cart', 'transfer_cart'];
    keys.forEach(key => sessionStorage.removeItem(key));
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
