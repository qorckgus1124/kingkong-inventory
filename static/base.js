// base.html 공통 스크립트 (모든 페이지에 똑같이 들어가던 인라인 스크립트를 파일로 뺐다 -> 브라우저가 한 번 받아 캐시)
function filterNavItems(query) {
  const q = query.trim().toLowerCase();
  document.querySelectorAll('#sidebarNav .nav-item').forEach(item => {
    const label = (item.getAttribute('data-label') || item.textContent).toLowerCase();
    item.classList.toggle('hidden-by-search', q.length > 0 && !label.includes(q));
  });
  document.querySelectorAll('#sidebarNav .nav-group').forEach(group => {
    const visible = group.querySelectorAll('.nav-item:not(.hidden-by-search)').length > 0;
    group.style.display = visible ? '' : 'none';
  });
}

// ==============================================================
// 모바일 전체 메뉴 오버레이 (PC에서는 mobile-bottom-nav 자체가 숨김 처리되어 진입 불가)
// 사이드바(#sidebarNav)의 모든 nav-group/nav-item을 그대로 복제해 항상 최신 메뉴를 보여준다.
// ==============================================================
function buildMobileNavOverlay() {
  const list = document.getElementById('mobileNavOverlayList');
  if (!list || list.dataset.filled) return;
  document.querySelectorAll('#sidebarNav .nav-group').forEach(group => {
    const clone = group.cloneNode(true);
    clone.querySelectorAll('.nav-item').forEach(item => item.classList.remove('hidden-by-search'));
    list.appendChild(clone);
  });
  list.dataset.filled = '1';
}

function openMobileNavMenu() {
  buildMobileNavOverlay();
  const overlay = document.getElementById('mobileNavOverlay');
  if (!overlay) return;
  overlay.classList.add('open');
  document.body.classList.add('mobile-nav-lock');
  const trigger = document.querySelector('.mobile-nav-more-btn');
  if (trigger) trigger.setAttribute('aria-expanded', 'true');
  const search = document.getElementById('mobileNavSearchInput');
  if (search) setTimeout(() => search.focus(), 150);
}

function closeMobileNavMenu() {
  const overlay = document.getElementById('mobileNavOverlay');
  if (!overlay) return;
  overlay.classList.remove('open');
  document.body.classList.remove('mobile-nav-lock');
  const trigger = document.querySelector('.mobile-nav-more-btn');
  if (trigger) trigger.setAttribute('aria-expanded', 'false');
}

// PC 폭으로 리사이즈되면 전체 메뉴 오버레이는 절대 노출되지 않도록 강제로 닫는다.
window.addEventListener('resize', function() {
  if (window.innerWidth > 768) closeMobileNavMenu();
});

function filterMobileNavItems(query) {
  const q = query.trim().toLowerCase();
  document.querySelectorAll('#mobileNavOverlayList .nav-item').forEach(item => {
    const label = (item.getAttribute('data-label') || item.textContent).toLowerCase();
    item.classList.toggle('hidden-by-search', q.length > 0 && !label.includes(q));
  });
  document.querySelectorAll('#mobileNavOverlayList .nav-group').forEach(group => {
    const visible = group.querySelectorAll('.nav-item:not(.hidden-by-search)').length > 0;
    group.style.display = visible ? '' : 'none';
  });
}

function updateThemeToggleUI() {
  const isDark = document.body.classList.contains('dark-mode');
  document.querySelectorAll('.theme-toggle-btn').forEach(btn => {
    // 유리 트랙/노브는 CSS의 ::before/::after로 그려지므로 innerHTML을 갈아도 유지된다.
    btn.innerHTML = isDark ? '<svg class="i3d" aria-hidden="true"><use href="#i3d-sun"></use></svg> 라이트 모드' : '<svg class="i3d" aria-hidden="true"><use href="#i3d-moon"></use></svg> 다크 모드';
    btn.setAttribute('role', 'switch');
    btn.setAttribute('aria-checked', isDark ? 'true' : 'false');
    btn.setAttribute('aria-label', isDark ? '라이트 모드로 전환' : '다크 모드로 전환');
  });
  if (typeof syncThemeColorMeta === 'function') syncThemeColorMeta();
}

function toggleTheme() {
  const body = document.body;
  body.classList.toggle('dark-mode');
  const isDark = body.classList.contains('dark-mode');
  localStorage.setItem('darkMode', isDark ? 'true' : 'false');
  updateThemeToggleUI();
}

// ==============================================================
// 전역 매장 드롭다운
// ==============================================================
let _globalStoreSelectLoaded = false;

// 선택한 매장은 브라우저에 저장되어 화면을 옮겨도 그대로 "귀속"된 상태로 유지된다.
// 바뀌는 시점은 딱 하나, 사용자가 이 드롭다운에서 다른 매장을 직접 고를 때다.
// 드롭다운에 이미 들어있는 매장 목록이 최신 목록과 같은지 확인한다.
function globalStoreOptionsMatch(sel, stores) {
  const current = Array.from(sel.options).map(o => o.value).filter(v => v !== '');
  if (current.length !== stores.length) return false;
  return stores.every((s, i) => current[i] === String(s.id));
}

// 저장된 귀속 매장을 드롭다운에 적용한다. 한 번도 고른 적이 없을 때만 첫 매장으로 정한다.
function applySavedGlobalStore(sel, stores) {
  const savedId = localStorage.getItem('global_store_id');
  if (savedId && stores.some(s => String(s.id) === String(savedId))) {
    sel.value = String(savedId);
  } else if (!savedId && stores.length > 0) {
    sel.value = String(stores[0].id);
    localStorage.setItem('global_store_id', String(stores[0].id));
  }
  return sel.value || null;
}

function applyGlobalStoreOptions(stores) {
  const sel = document.getElementById('globalStoreSelect');
  if (!sel || !Array.isArray(stores) || stores.length === 0) return null;

  // 목록이 그대로면 다시 그리지 않는다 (화면 깜빡임 방지)
  if (!globalStoreOptionsMatch(sel, stores)) {
    let options = '<option value="">매장 선택</option>';
    stores.forEach(s => {
      options += `<option value="${s.id}">${escapeHtml(s.name)}</option>`;
    });
    sel.innerHTML = options;
  }
  return applySavedGlobalStore(sel, stores);
}

async function loadGlobalStoreSelect() {
  const sel = document.getElementById('globalStoreSelect');
  if (!sel) return;

  // 1단계: 이미 화면에 들어있는 목록(서버가 미리 넣어준 것) 또는 저장해둔 목록으로
  //        즉시 적용한다. 네트워크를 기다리지 않으므로 "매장 선택"이 스쳐 보이지 않는다.
  const serverStores = Array.isArray(window.__GLOBAL_STORES) ? window.__GLOBAL_STORES : null;
  const instant = serverStores && serverStores.length > 0
    ? serverStores
    : ((typeof getStoresInstant === 'function') ? getStoresInstant() : null);
  if (instant && instant.length > 0) {
    applyGlobalStoreOptions(instant);
    // 각 화면의 매장 드롭다운도 같은 값으로 맞춘다. 이때는 change를 발생시키지 않으므로
    // 목록을 다시 불러오는 중복 조회가 생기지 않는다.
    syncAllStoreDropdowns(sel.value);
    _globalStoreSelectLoaded = true;
    return;   // 서버가 준 목록이 최신이므로 추가 요청이 필요 없다
  }

  // 2단계: 준비된 목록이 없을 때만 서버에 물어본다.
  try {
    const stores = await api('/api/stores');
    if (!Array.isArray(stores) || stores.length === 0) return;
    const before = sel.value;
    applyGlobalStoreOptions(stores);
    _globalStoreSelectLoaded = true;
    // 선택값이 실제로 바뀐 경우에만 다른 드롭다운을 맞춘다.
    if (sel.value !== before) syncAllStoreDropdowns(sel.value);
  } catch (e) {
    console.error('전역 매장 로드 오류:', e);
  }
}

document.addEventListener('DOMContentLoaded', function() {
  const globalSel = document.getElementById('globalStoreSelect');
  if (globalSel) {
    // 사용자가 직접 매장을 바꾼 순간에만 귀속 매장이 변경된다.
    globalSel.addEventListener('change', function() {
      const val = this.value;
      localStorage.setItem('global_store_id', val);

      // 화면의 매장 드롭다운에 change를 전달한다 (장바구니 매장 확인 등 후속 처리 포함).
      // 그 드롭다운의 change 처리기가 이미 목록을 다시 불러오므로,
      // 아래 load 함수들을 또 호출하면 같은 조회가 두 번 나간다.
      const changed = syncAllStoreDropdowns(val, true);
      if (changed > 0) return;

      // 매장 드롭다운이 없는 화면(대시보드/일일보고 등)만 여기서 새로 불러온다.
      if (typeof load === 'function') load();
      if (typeof loadDashboard === 'function') loadDashboard();
      if (typeof loadProducts === 'function') loadProducts();
      if (typeof loadData === 'function') loadData();
      if (typeof loadRecommend === 'function') loadRecommend();
      if (typeof loadForecast === 'function') loadForecast();
      if (typeof loadBestsellers === 'function' && typeof _bestsellerPeriod !== 'undefined') loadBestsellers(_bestsellerPeriod);
      if (typeof loadDailySalesSummary === 'function') loadDailySalesSummary();
      if (typeof loadAllReports === 'function') loadAllReports();
    });
  }

  updateThemeToggleUI();

  loadFloatingRevenue();
  startTodayRevenueAutoRefresh();
  restoreFloatingPanelState();
  loadHandoverMemo();
  loadGlobalStoreSelect();
});

// 각 화면의 매장 드롭다운을 전역 선택값으로 맞춘다.
//  - fireChange=false (기본): 값만 조용히 맞춘다. 화면을 열 때 쓰는 경로로,
//    예전처럼 change 이벤트를 강제로 쏘지 않으므로 목록을 두 번 불러오지 않는다.
//  - fireChange=true: 사용자가 전역 드롭다운에서 매장을 직접 바꾼 경우.
//    값이 "실제로 달라진" 드롭다운에만 change를 보내서 각 화면의 후속 처리
//    (장바구니 매장 확인, 검색어 초기화 등)가 한 번만 실행되게 한다.
// 반환값: change를 실제로 발생시킨 드롭다운 개수
function syncAllStoreDropdowns(storeId, fireChange) {
  if (!storeId) return 0;
  const targetIds = [
    'f_store', 'pre_store', 'filterStore', 'storeSelect',
    'fromStore', 'toStore', 'movementStoreFilter',
    'uploadStoreSelect', 'recordStoreSelect', 'outboundStoreSelect'
  ];
  let changed = 0;
  targetIds.forEach(id => {
    const el = document.getElementById(id);
    if (el && el.options.length > 0) {
      const exists = Array.from(el.options).some(opt => opt.value == storeId);
      if (exists && el.value != storeId) {
        el.value = storeId;
        if (fireChange) {
          el.dispatchEvent(new Event('change', { bubbles: true }));
          changed++;
        }
      }
    }
  });
  document.querySelectorAll('select[name="store_id"]').forEach(el => {
    if (el.options.length > 0) {
      const exists = Array.from(el.options).some(opt => opt.value == storeId);
      if (exists) el.value = storeId;
    }
  });
  return changed;
}

// ==============================================================
// 인수인계 메모장 (대시보드 카드에서만 조회/수정 가능)
// ==============================================================
async function loadHandoverMemo() {
  try {
    const data = await api('/api/settings');
    const memo = data.handover_memo || '';
    // 대시보드 카드(텍스트에어리어)에 값을 채워넣는다. 다른 페이지에는 해당 요소가 없으므로 조용히 넘어간다.
    const dashCard = document.getElementById('dashboardMemoContent');
    if (dashCard) dashCard.value = memo;
  } catch (e) {
    console.error('메모 로드 오류:', e);
  }
}

async function saveHandoverMemo() {
  const dashCard = document.getElementById('dashboardMemoContent');
  if (!dashCard) return;
  const memo = dashCard.value;
  const status = document.getElementById('handoverSaveStatus');
  if (status) status.textContent = '저장 중...';
  try {
    await api('/api/settings', {
      method: 'POST',
      body: JSON.stringify({ handover_memo: memo })
    });
    if (status) {
      status.textContent = '✅ 저장 완료';
      setTimeout(() => { status.textContent = ''; }, 2000);
    }
  } catch (e) {
    console.error('메모 저장 오류:', e);
    if (status) {
      status.textContent = '❌ 저장 실패';
      setTimeout(() => { status.textContent = ''; }, 2000);
    }
  }
}

// ==============================================================
// 플로팅 패널 매출
// ==============================================================
function toggleFloatingPanel() {
  const body = document.getElementById('floatingPanelBody');
  const toggle = document.getElementById('floatingPanelToggle');
  const folded = body.style.display !== 'none';
  body.style.display = folded ? 'none' : '';
  toggle.textContent = folded ? '▼' : '▲';
  localStorage.setItem('floatingPanelFolded', folded ? 'true' : 'false');
}

function restoreFloatingPanelState() {
  const isFolded = localStorage.getItem('floatingPanelFolded') === 'true';
  if (isFolded) {
    document.getElementById('floatingPanelBody').style.display = 'none';
    document.getElementById('floatingPanelToggle').textContent = '▼';
  }
}

// ⚡ 예전에는 오늘 매출 하나를 보려고 무거운 /api/dashboard 전체를 불러왔다.
//    가벼운 /api/today_revenue로 바꿔서 자주 갱신해도 부담이 없게 했다.
//    이 값은 캐시를 타지 않으므로 판매를 등록하면 곧바로 반영된다.
async function loadFloatingRevenue() {
  try {
    // 전역 매장 선택이 있으면 해당 매장 기준으로, 없으면 전체 매장 기준으로 오늘 매출을 가져온다.
    const globalStoreId = localStorage.getItem('global_store_id');
    const url = globalStoreId ? `/api/today_revenue?store_id=${globalStoreId}` : '/api/today_revenue';
    const data = await api(url);
    // 응답을 받지 못했으면 왜 못 받았는지 콘솔에 남기고, 화면에는 실패 표시를 한다.
    if (!data || data.__apiError || !data.today) {
      console.error('오늘 매출 응답 없음:', data && data.__apiError ? data.__apiError : data);
      document.getElementById('fpRevenue').textContent = '--원';
      return;
    }
    if (Array.isArray(data.warnings) && data.warnings.length > 0) {
      console.warn('오늘 매출 일부 계산 실패:', data.warnings.join(' / '));
    }
    const revenue = data.today?.revenue || 0;
    document.getElementById('fpRevenue').textContent = fmt(revenue) + '원';

    // 대시보드가 열려 있으면 오늘 카드도 같이 실시간으로 갱신한다.
    const todayRevenueEl = document.getElementById('todayRevenue');
    if (todayRevenueEl) todayRevenueEl.textContent = fmt(revenue) + '원';
    const todayProfitEl = document.getElementById('todayProfit');
    if (todayProfitEl) todayProfitEl.textContent = fmt(data.today?.profit ?? 0) + '원';

    const target = data.monthly_target || {};
    const targetAmount = Number(target.target) || 0;
    const currentRevenue = Number(target.current_revenue) || 0;
    const remaining = Number(target.remaining_amount) || 0;
    const dailyNeeded = Number(target.daily_avg_needed) || 0;
    const progress = Number(target.progress_percent) || 0;   // 혹시 문자열로 오더라도 숫자로 변환
    const isAchieved = !!target.is_achieved;

    document.getElementById('fpTargetAmount').textContent = fmt(targetAmount) + '원';
    document.getElementById('fpTargetProgress').textContent = progress.toFixed(1) + '%';
    document.getElementById('fpRemaining').textContent = isAchieved ? '✅ 달성!' : fmt(remaining) + '원';
    document.getElementById('fpDailyNeeded').textContent = isAchieved ? '🎉 완료!' : fmt(Math.ceil(dailyNeeded)) + '원/일';

    const remainingEl = document.getElementById('fpRemaining');
    if (!isAchieved && remaining > 0) {
      remainingEl.style.color = 'var(--danger)';
      remainingEl.style.fontWeight = '700';
    } else if (isAchieved) {
      remainingEl.style.color = 'var(--accent)';
      remainingEl.style.fontWeight = '700';
    }

    const targetInfo = document.getElementById('fpTargetInfo');
    let progressBar = targetInfo.querySelector('.fp-progress-bar');
    if (!progressBar) {
      progressBar = document.createElement('div');
      progressBar.className = 'fp-progress-bar';
      progressBar.style.cssText = `
        width: 100%; height: 6px; background: var(--hover-bg);
        border: 1px solid var(--border); border-radius: 999px;
        margin-top: 6px; overflow: hidden;
      `;
      const fill = document.createElement('div');
      fill.className = 'fp-progress-fill';
      fill.style.cssText = `
        width: 0%; height: 100%;
        background: ${isAchieved ? 'var(--success)' : 'var(--accent)'};
        border-radius: 999px; transition: width 0.5s ease;
      `;
      progressBar.appendChild(fill);
      targetInfo.appendChild(progressBar);
    }
    const fill = progressBar.querySelector('.fp-progress-fill');
    if (fill) {
      fill.style.width = Math.min(progress, 100) + '%';
      fill.style.background = isAchieved ? 'var(--success)' : 'var(--accent)';
    }
  } catch (e) {
    console.error('플로팅 패널 매출 로드 오류:', e);
    document.getElementById('fpRevenue').textContent = '--원';
    document.getElementById('fpTargetAmount').textContent = '--';
    document.getElementById('fpTargetProgress').textContent = '--';
    document.getElementById('fpRemaining').textContent = '--';
    document.getElementById('fpDailyNeeded').textContent = '--';
  }
}

// ==============================================================
// 오늘 매출 실시간 갱신
// ==============================================================
// 1) 20초마다 자동 갱신 (다른 기기에서 판매가 등록돼도 반영된다)
// 2) 화면을 다시 보는 순간 즉시 갱신 (탭 전환 후 옛 숫자가 남지 않도록)
// 3) 판매/입출고 등록 직후 즉시 갱신 (common.js의 api()가 호출해 준다)
// 화면이 가려져 있을 때는 요청을 보내지 않아 불필요한 부하가 없다.
const TODAY_REVENUE_REFRESH_MS = 20000;
let _todayRevenueTimer = null;
let _todayRevenueBusy = false;

async function refreshTodayRevenue() {
  if (_todayRevenueBusy) return;
  if (document.visibilityState === 'hidden') return;
  _todayRevenueBusy = true;
  try {
    await loadFloatingRevenue();
  } finally {
    _todayRevenueBusy = false;
  }
}
window.refreshTodayRevenue = refreshTodayRevenue;

function startTodayRevenueAutoRefresh() {
  if (_todayRevenueTimer) clearInterval(_todayRevenueTimer);
  _todayRevenueTimer = setInterval(refreshTodayRevenue, TODAY_REVENUE_REFRESH_MS);
}

document.addEventListener('visibilitychange', function () {
  if (document.visibilityState === 'visible') refreshTodayRevenue();
});

// 키보드 단축키
document.addEventListener('keydown', function(e) {
  if (e.ctrlKey && e.key === 'Enter') {
    const form = document.querySelector('form');
    if (form) {
      const submitBtn = form.querySelector('button[type="submit"], .primary');
      if (submitBtn) submitBtn.click();
    }
    const saveBtn = document.querySelector('button.primary[onclick*="save"], button.primary[onclick*="add"], button.primary[onclick*="register"], button.primary[onclick*="checkout"]');
    if (saveBtn) saveBtn.click();
    e.preventDefault();
  }
  if (e.ctrlKey && e.shiftKey && e.key === 'S') {
    window.location.href = '/sales';
    e.preventDefault();
  }
  if (e.ctrlKey && e.shiftKey && e.key === 'P') {
    window.location.href = '/';
    e.preventDefault();
  }
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal, .modal-overlay, [style*="display:flex"][style*="position:fixed"]').forEach(el => {
      if (el.style.display !== 'none') {
        el.style.display = 'none';
      }
    });
    closeMobileNavMenu();
  }
  if (e.key === '/' && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
    const searchInput = document.querySelector('input[type="text"][placeholder*="검색"], input[placeholder*="검색"], #searchBox');
    if (searchInput) {
      searchInput.focus();
      e.preventDefault();
    }
  }
});

if ('serviceWorker' in navigator) {
  // 예전 버전은 /static/sw.js로 등록되어 스코프가 /static/ 이하로 제한되어 있었다.
  // 이제 루트(/)에서 서비스 워커를 등록하므로, 남아있는 예전 등록은 정리한다.
  navigator.serviceWorker.getRegistrations().then(function (regs) {
    regs.forEach(function (reg) {
      if (reg.scope.endsWith('/static/')) reg.unregister();
    });
  });
  navigator.serviceWorker.register('/sw.js')
    .then(function() { console.log('✅ 서비스 워커 등록 완료'); })
    .catch(function() { console.log('⚠️ 서비스 워커 등록 실패'); });
}

// ==============================================================
// PWA 홈 화면 추가 유도 배너
// ==============================================================
(function () {
  const DISMISS_KEY = 'pwa_install_dismissed_at';
  const DISMISS_DAYS = 14;
  let deferredPrompt = null;

  function isStandalone() {
    return window.matchMedia('(display-mode: standalone)').matches
      || window.navigator.standalone === true; // iOS 홈화면 실행 여부
  }

  function recentlyDismissed() {
    const t = localStorage.getItem(DISMISS_KEY);
    if (!t) return false;
    const days = (Date.now() - Number(t)) / (1000 * 60 * 60 * 24);
    return days < DISMISS_DAYS;
  }

  function showBanner(subText) {
    if (isStandalone() || recentlyDismissed()) return;
    const banner = document.getElementById('pwaInstallBanner');
    if (!banner) return;
    if (subText) document.getElementById('pwaInstallSub').textContent = subText;
    banner.style.display = 'flex';
  }

  window.pwaDismissBanner = function () {
    localStorage.setItem(DISMISS_KEY, String(Date.now()));
    const banner = document.getElementById('pwaInstallBanner');
    if (banner) banner.style.display = 'none';
  };

  window.pwaInstallClick = async function () {
    if (deferredPrompt) {
      deferredPrompt.prompt();
      await deferredPrompt.userChoice;
      deferredPrompt = null;
      pwaDismissBanner();
    } else {
      // iOS 등 beforeinstallprompt를 지원하지 않는 브라우저: 수동 안내만 하고 닫기
      pwaDismissBanner();
    }
  };

  // Android/Chrome: 브라우저가 설치 가능하다고 판단하면 이 이벤트가 발생한다.
  window.addEventListener('beforeinstallprompt', function (e) {
    e.preventDefault();
    deferredPrompt = e;
    showBanner('홈 화면에 추가하면 앱처럼 빠르게 열 수 있어요');
  });

  // iOS Safari는 beforeinstallprompt가 없으므로, UA로 감지해서 수동 안내 배너를 보여준다.
  document.addEventListener('DOMContentLoaded', function () {
    const ua = window.navigator.userAgent;
    const isIOS = /iphone|ipad|ipod/i.test(ua);
    const isSafari = /safari/i.test(ua) && !/crios|fxios|edgios/i.test(ua);
    if (isIOS && isSafari && !isStandalone()) {
      const btn = document.getElementById('pwaInstallBtn');
      if (btn) btn.textContent = '방법 보기';
      window.pwaInstallClick = function () {
        toast('공유 버튼 🔗 → "홈 화면에 추가"를 눌러주세요', false, 'ℹ️');
        pwaDismissBanner();
      };
      showBanner('공유 버튼(하단) → "홈 화면에 추가"로 앱처럼 사용하세요');
    }
  });
})();
